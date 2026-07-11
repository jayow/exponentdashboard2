#!/usr/bin/env bash
# Daily refresh — runs the full extract → dbt → serve chain, then commits +
# pushes the regenerated web/public/*.json so the deployed dashboard updates.
# Same logic as .github/workflows/refresh.yml.
#
# Runs in two environments:
#   * Mac (launchd):    REPO derived from script location, .env from disk,
#                       python+dbt via .venv. Triggered by
#                       ~/Library/LaunchAgents/com.exponent.refresh.local.plist.
#   * Railway (cron):   REPO=/app, env vars injected by Railway, python+dbt
#                       from the Docker image's system python (no .venv).
#                       /app/data is a persistent volume.
set -u

# Derive repo root from script location so this works on any machine.
SCRIPT_DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" && pwd )"
REPO="$( cd "$SCRIPT_DIR/.." && pwd )"
cd "$REPO"

# Mac: load .env. Railway: env vars are already in process env, skip.
if [ -f .env ] && [ -z "${RAILWAY_PROJECT_ID:-}" ]; then
  set -a
  source .env
  set +a
fi

# Pick python + dbt — venv on Mac, system on Railway.
if [ -x "$REPO/.venv/bin/python" ]; then
  PY="$REPO/.venv/bin/python"
  DBT="$REPO/.venv/bin/dbt"
else
  PY="$(command -v python3 || command -v python)"
  DBT="$(command -v dbt)"
fi

echo ""
echo "===== Refresh started: $(date) ====="

# Railway gotcha: the persistent volume can be mounted up to ~30 seconds
# AFTER the container starts (we observed it in our first run — extract_markets
# wrote to ephemeral disk before /app/data was mounted). Detect the mount
# explicitly by waiting for it to be a mountpoint. On Mac this exits
# immediately because we're not in Railway.
if [ -n "${RAILWAY_PROJECT_ID:-}" ]; then
  for i in 1 2 3 4 5 6 7 8 9 10 11 12; do
    if mountpoint -q data 2>/dev/null; then
      echo "  volume mounted on data/ after ${i}x5s"; break
    fi
    sleep 5
  done
fi

# Railway-only: warehouse is on the persistent volume mounted at
# data/, but the volume starts empty. Seed it from the latest GH
# release if missing OR clearly broken (an earlier failed run left an
# empty file behind, which then made extract_transactions try to
# backfill 777K sigs from scratch and stall).
if command -v gh >/dev/null 2>&1; then
  # Disk space sanity print so future failures are obvious from logs.
  echo "  volume usage: $(df -h data 2>/dev/null | tail -1)"

  needs_seed=0
  if [ ! -f data/warehouse.duckdb ]; then
    needs_seed=1
  else
    # ALSO check available disk space — a failed run can fill the volume
    # with partial tx payloads (raw_helius_tx may show plenty of rows but
    # DuckDB still can't write a checkpoint because there's nowhere to
    # put it). df reports in 1K blocks. Force re-seed if <500 MB free.
    disk_free_kb=$(df data 2>/dev/null | tail -1 | awk '{print $4}')
    echo "  disk free: ${disk_free_kb:-?} KB ($(( ${disk_free_kb:-0} / 1024 )) MB)"
    if [ "${disk_free_kb:-0}" -lt 524288 ]; then
      needs_seed=1
      echo "  volume nearly full (< 500 MB free) — forcing wipe + re-seed"
    else
      # Otherwise check actual data freshness — a failed run can leave a
      # warehouse with metadata but no raw_helius_tx, which then makes
      # extract_transactions try to backfill all 700K+ historical sigs.
      # Trust the seed only if it has tx history.
      tx_count=$("$PY" -c "import duckdb; con=duckdb.connect('data/warehouse.duckdb', read_only=True); print(con.execute('SELECT COUNT(*) FROM raw_helius_tx').fetchone()[0])" 2>/dev/null || echo 0)
      echo "  raw_helius_tx row count: $tx_count"
      if [ "$tx_count" -lt 1000 ]; then
        needs_seed=1
        echo "  warehouse has $tx_count raw_helius_tx rows — needs re-seed"
      fi
    fi
  fi
  if [ "$needs_seed" = 1 ]; then
    echo "Wiping data/ (volume may be full from a prior failed run) and re-seeding…"
    # rm -rf data/* clears ANY leftover from prior runs — broken warehouse,
    # incomplete .gz downloads, etc. Required because a 5 GB volume that's
    # full from a previous backfill attempt won't fit a new 1.3 GB seed
    # download otherwise.
    rm -rf data/*
    mkdir -p data
    echo "  after wipe: $(df -h data 2>/dev/null | tail -1)"
    # -R required because we run BEFORE git bootstrap — without it, gh tries
    # to detect the repo from local .git which doesn't exist yet.
    gh release download warehouse-seed -R jayow/exponentdashboard2 -p 'warehouse_slim.duckdb.gz' -D data/ \
      && gunzip data/warehouse_slim.duckdb.gz \
      && mv data/warehouse_slim.duckdb data/warehouse.duckdb \
      && echo "Seeded: $(du -sh data/warehouse.duckdb | cut -f1)" \
      || echo "  SEED FAILED — extractors will operate on empty warehouse, which will stall extract_signatures"
  fi
fi

# Railway-only: bootstrap git from scratch — Railway's build context strips
# the .git/ directory before sending to Docker (regardless of .dockerignore),
# so the image has the working tree but no history. We re-init here and point
# at origin/main as the parent commit so `git diff --cached` only sees the
# regenerated web/public/*.json files, and `git push origin HEAD:main` works.
if [ -n "${RAILWAY_PROJECT_ID:-}" ] && [ ! -d .git ]; then
  echo "Bootstrapping git in /app (Railway strips .git from the image)"
  git init -q -b main
  git config user.name  "${GIT_COMMITTER_NAME:-railway-bot}"
  git config user.email "${GIT_COMMITTER_EMAIL:-railway-bot@users.noreply.github.com}"
  if [ -n "${GH_TOKEN:-}" ]; then
    # Fail FAST if the token is dead — otherwise the run burns an hour of
    # extraction and only discovers the expired token at the final push.
    if ! curl -sf -o /dev/null -H "Authorization: Bearer ${GH_TOKEN}" \
         https://api.github.com/repos/jayow/exponentdashboard2; then
      echo "FATAL: GH_TOKEN is invalid or expired — update the Railway service variable."
      exit 1
    fi
    git remote add origin "https://x-access-token:${GH_TOKEN}@github.com/jayow/exponentdashboard2.git"
  else
    echo "FATAL: GH_TOKEN is not set — the refresh cannot push. Set it in Railway service variables."
    exit 1
  fi
  # Fetch origin's main branch and align HEAD with it without touching files
  # (the working tree IS the commit Railway built from; --mixed only updates
  # HEAD + index). After this, the only diffs are our regenerated JSON.
  git fetch -q origin main 2>&1 | head -3 || echo "  (initial fetch failed — push will probably also fail)"
  git reset -q --mixed FETCH_HEAD 2>&1 | head -3 || echo "  (reset failed)"
fi

# Pull any commits the GHA bot (or another local run) made in the meantime so
# we don't push a fast-forward conflict. Skip on Railway — the bootstrap above
# already fetched + reset to origin/main, and pip-install side-effects
# (*.egg-info/, __pycache__) leave the working tree with unstaged noise that
# would block `pull --rebase`.
if [ -z "${RAILWAY_PROJECT_ID:-}" ]; then
  git pull --rebase --quiet origin main || echo "  (git pull skipped — non-fatal)"
fi

# Per-extractor failure isolation: one extractor blowing up (Helius 429, etc.)
# logs a warning and the loop continues. dbt + serve still ship partial data.
failed=0
run() {
  name="$1"; shift
  echo ""
  echo "--- $name ---"
  "$@"
  rc=$?
  if [ $rc -ne 0 ]; then
    echo "WARN: $name exited $rc — continuing with remaining steps"
    failed=$((failed + 1))
  fi
}

# Strategy vaults FIRST — their getProgramAccounts calls are the most
# 429-prone, so run them while the RPC key is cold (2026-07-07/08 GHA runs
# 429'd all gPA extractors after an hour of tx fetching). states before
# obligations/holders; actions after signatures; executions after both.
run extract_sv_registry     "$PY" -m extract_load.extract_strategy_vault_registry
run extract_sv_states       "$PY" -m extract_load.extract_strategy_vault_states
run extract_sv_obligations  "$PY" -m extract_load.extract_strategy_vault_obligations
run extract_sv_proposals    "$PY" -m extract_load.extract_strategy_vault_proposals
run extract_sv_withdrawals  "$PY" -m extract_load.extract_strategy_vault_withdrawals
run extract_sv_signatures   "$PY" -m extract_load.extract_strategy_vault_signatures
run extract_sv_actions      "$PY" -m extract_load.extract_strategy_vault_actions
run extract_sv_executions   "$PY" -m extract_load.extract_strategy_vault_executions
run extract_sv_holders      "$PY" -m extract_load.extract_strategy_vault_holders
run extract_mint_supplies   "$PY" -m extract_load.extract_mint_supplies
run extract_markets         "$PY" -m extract_load.extract_markets
run extract_signatures      "$PY" -m extract_load.extract_signatures
run extract_transactions    "$PY" -m extract_load.extract_transactions
run extract_token_metadata  "$PY" -m extract_load.extract_token_metadata
run extract_prices          "$PY" -m extract_load.extract_prices
run extract_positions       "$PY" -m extract_load.extract_positions
run extract_vault_states    "$PY" -m extract_load.extract_vault_states
run extract_market_three    "$PY" -m extract_load.extract_market_three_states
run extract_tranche_states  "$PY" -m extract_load.extract_tranche_states
run extract_tranche_actions "$PY" -m extract_load.extract_tranche_actions
run extract_pool_state      "$PY" -m extract_load.extract_pool_state
run extract_holders         "$PY" -m extract_load.extract_holders
run extract_anchor_events   "$PY" -m extract_load.extract_anchor_events
# extract_clmm_claim_events is parked: ClaimFarmEmissions instructions only
# fire from the Core program (Exponentna...), not CLMM (XPC1MM4d). The CLMM
# IDL declares the event types but the program doesn't actually emit them.
# Re-enable once we identify the actual CLMM farm-claim path.
run extract_lst_rates       "$PY" -m extract_load.extract_lst_rates
run extract_v2_markets      "$PY" -m extract_load.extract_v2_markets
run extract_v2_books        "$PY" -m extract_load.extract_v2_books

if [ $failed -gt 0 ]; then
  echo ""
  echo "WARN: $failed extractor(s) failed — dbt + serve will run on partial data"
fi

echo ""
echo "--- dbt deps (install packages from packages.yml if missing) ---"
if [ ! -d transform/dbt_packages ]; then
  (cd transform && DBT_PROFILES_DIR=. "$DBT" deps) || echo "WARN: dbt deps failed (will try build anyway)"
fi

echo ""
echo "--- dbt build ---"
(cd transform && DBT_PROFILES_DIR=. "$DBT" build) || { echo "FATAL: dbt build failed"; exit 1; }

echo ""
echo "--- serve.build_web_data ---"
"$PY" -m serve.build_web_data || echo "WARN: serve.build_web_data failed (continuing)"

# Accuracy gate (docs/ACCURACY.md): fast in-warehouse checks. WARN is fine
# (known/informational); a FAIL means genuinely bad data (negative TVL, broken
# invariant, decode error, supply-drift blow-out) → refuse to publish it.
echo ""
echo "--- accuracy checks ---"
acc_fail=0
"$PY" -m ops.accuracy_check || acc_fail=1

# Railway: NULL out old raw_helius_tx.payload to keep the 5 GB volume from
# filling up over time. Same logic the GHA workflow runs after each
# refresh. Mac doesn't need this (40 GB+ disk, no constraint).
if [ -n "${RAILWAY_PROJECT_ID:-}" ]; then
  echo ""
  echo "--- compact warehouse (strip old payloads) ---"
  "$PY" -m extract_load.compact_in_place || echo "WARN: compact failed (continuing)"
  echo "  volume after compact: $(df -h data 2>/dev/null | tail -1)"
fi

echo ""
echo "--- commit + push ---"
if [ "$acc_fail" = 1 ]; then
  echo "REFUSING TO PUBLISH: accuracy checks reported a FAIL (see above / docs/ACCURACY.md)."
  echo "Fix the data issue and re-run. web/public left uncommitted."
  echo ""
  echo "===== Refresh finished (publish blocked on accuracy): $(date) ====="
  exit 1
fi
git add web/public/*.json web/public/wallet/ web/public/strategy-txns/ extract_load/known_vaults.json
if git diff --cached --quiet; then
  echo "No JSON changes to commit."
else
  git commit -m "chore: local refresh $(date -u +%Y-%m-%d)"
  # Rebase onto origin before pushing: GHA's nightly run now finishes AFTER
  # this job starts (its runs crossed past 01:00 UTC as the pipeline grew),
  # so main has usually moved since our bootstrap and a bare push is
  # non-fast-forward — this killed every Railway publish at the last step.
  # -X theirs keeps OUR freshly-built JSONs on conflict (during a rebase,
  # 'theirs' = the commit being replayed, i.e. ours — and this build is the
  # newer one). Two attempts, then give up loudly but exit 0: this is the
  # backup runner; the primary already published today.
  pushed=false
  for attempt in 1 2; do
    git fetch -q origin main || true
    git rebase -X theirs FETCH_HEAD >/dev/null 2>&1 || { git rebase --abort 2>/dev/null; break; }
    if git push origin HEAD:main; then pushed=true; echo "Pushed."; break; fi
  done
  if [ "$pushed" != true ]; then
    echo "WARN: push failed after rebase+retry — main moved again or auth issue. Backup publish skipped."
  fi
fi

echo ""
echo "===== Refresh finished: $(date) ====="
# trigger rebuild 2026-06-02T17:57:49Z
