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
# release if missing. (Mac already has data/warehouse.duckdb.)
if [ ! -f data/warehouse.duckdb ] && command -v gh >/dev/null 2>&1; then
  echo "Seeding warehouse.duckdb from GH release warehouse-seed…"
  mkdir -p data
  gh release download warehouse-seed -p 'warehouse_slim.duckdb.gz' -D data/ \
    && gunzip data/warehouse_slim.duckdb.gz \
    && mv data/warehouse_slim.duckdb data/warehouse.duckdb \
    && echo "Seeded: $(du -sh data/warehouse.duckdb | cut -f1)"
fi

# Railway-only: configure git identity + credential helper so commits can be
# made + pushed. GH_TOKEN env var grants repo push perms.
if [ -n "${RAILWAY_PROJECT_ID:-}" ]; then
  git config user.name  "${GIT_COMMITTER_NAME:-railway-bot}"
  git config user.email "${GIT_COMMITTER_EMAIL:-railway-bot@users.noreply.github.com}"
  if [ -n "${GH_TOKEN:-}" ]; then
    git config credential.helper '!f() { echo "username=x-access-token"; echo "password=${GH_TOKEN}"; }; f'
  fi
fi

# Pull any commits the GHA bot (or another local run) made in the meantime so
# we don't push a fast-forward conflict.
git pull --rebase --quiet origin main || echo "  (git pull skipped — non-fatal)"

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

run extract_markets         "$PY" -m extract_load.extract_markets
run extract_signatures      "$PY" -m extract_load.extract_signatures
run extract_transactions    "$PY" -m extract_load.extract_transactions
run extract_token_metadata  "$PY" -m extract_load.extract_token_metadata
run extract_prices          "$PY" -m extract_load.extract_prices
run extract_positions       "$PY" -m extract_load.extract_positions
run extract_pool_state      "$PY" -m extract_load.extract_pool_state
run extract_holders         "$PY" -m extract_load.extract_holders
run extract_anchor_events   "$PY" -m extract_load.extract_anchor_events
run extract_lst_rates       "$PY" -m extract_load.extract_lst_rates
run extract_v2_markets      "$PY" -m extract_load.extract_v2_markets
run extract_v2_books        "$PY" -m extract_load.extract_v2_books

if [ $failed -gt 0 ]; then
  echo ""
  echo "WARN: $failed extractor(s) failed — dbt + serve will run on partial data"
fi

echo ""
echo "--- dbt build ---"
(cd transform && DBT_PROFILES_DIR=. "$DBT" build) || { echo "FATAL: dbt build failed"; exit 1; }

echo ""
echo "--- serve.build_web_data ---"
"$PY" -m serve.build_web_data || echo "WARN: serve.build_web_data failed (continuing)"

echo ""
echo "--- commit + push ---"
git add web/public/*.json
if git diff --cached --quiet; then
  echo "No JSON changes to commit."
else
  git commit -m "chore: local refresh $(date -u +%Y-%m-%d)"
  git push origin HEAD:main && echo "Pushed."
fi

echo ""
echo "===== Refresh finished: $(date) ====="
