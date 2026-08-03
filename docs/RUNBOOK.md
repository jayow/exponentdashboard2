# Runbook

## First-time setup

```bash
cd ExponentDashboard2
python3 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
cp .env.example .env                          # fill in HELIUS_KEY_1 + HELIUS_KEY_2
cd transform && dbt deps && cd ..
```

## Daily refresh

```bash
make all          # extract (incremental) → transform (incremental) → serve (slim JSONs)
make refresh      # same as 'all' but writes a timestamped log to data/logs/refresh.log
```

> **Keep the Makefile's `extract` target in sync with `.github/workflows/refresh.yml`
> and `ops/refresh_local.sh`.** They are three hand-maintained copies of one list and
> they drifted: the Makefile was missing `extract_mint_supplies`, `extract_tranche_states`,
> `extract_tranche_actions`, `extract_lst_rates`, `extract_v2_markets`, `extract_v2_books`.
> Because `extract_mint_supplies` never ran, `make refresh` produced **no authoritative
> supply snapshot** — the one thing that catches reconstruction drift — and a 21-day-stale
> snapshot let a 33% SY error reach production. Nothing warns you about this; the run
> exits 0. After editing any of the three, diff them:
>
> ```bash
> diff <(sed -n '/^extract:/,/^$/p' Makefile | grep -o 'extract_load\.[a-z_0-9]*' | sort) \
>      <(grep -oE 'extract_load\.extract_[a-z_0-9]+' .github/workflows/refresh.yml | sort -u)
> ```
>
> `make refresh` also does **not** run `ops.accuracy_check` (only `refresh.yml` does),
> so a local refresh publishes nothing-checked unless you run it yourself.

Both are idempotent and safe to run repeatedly. Performance:
- First full backfill: ~4 h (568K txs from Helius)
- Steady-state incremental: **~2 min** total

Incremental materialization breakdown:
- `extract_signatures`     ~30 s  (only new sigs via `until=<newest_known>`)
- `extract_transactions`   ~30 s  (anti-join on raw_signatures vs raw_helius_tx)
- `extract_markets/tokens/prices` ~10 s (all idempotent upserts)
- `dbt build`              ~60 s  (stg_inner_ix + stg_token_changes process only
                                    last 24h via `block_time >= max - 1 day`)
- `serve/build_web_data`   ~5 s

## Scheduling on macOS

Daily at 06:00 local via `launchd`:

```bash
# 1. Edit ops/com.hanyon.exponent-dashboard.refresh.plist:
#    Change WorkingDirectory and log paths to match your local clone.

# 2. Install:
cp ops/com.hanyon.exponent-dashboard.refresh.plist ~/Library/LaunchAgents/
launchctl load ~/Library/LaunchAgents/com.hanyon.exponent-dashboard.refresh.plist

# 3. Verify with a manual trigger:
launchctl start com.hanyon.exponent-dashboard.refresh
tail -f data/logs/refresh.log

# 4. To change schedule: edit StartCalendarInterval in the plist + reload:
launchctl unload ~/Library/LaunchAgents/com.hanyon.exponent-dashboard.refresh.plist
launchctl load ~/Library/LaunchAgents/com.hanyon.exponent-dashboard.refresh.plist

# 5. To stop running daily:
launchctl unload ~/Library/LaunchAgents/com.hanyon.exponent-dashboard.refresh.plist
```

The plist runs `bash -l -c 'source .venv/bin/activate && make refresh'`, which:
- Picks up your shell PATH (so Homebrew Python/dbt resolve)
- Activates the venv
- Appends one timestamped block per run to `data/logs/refresh.log`
- launchd's raw stdout/stderr go to `data/logs/launchd.{out,err}.log` (should be empty in steady state)

If your Mac sleeps at 06:00, launchd will run the job at the next wake. Set
"Wake for network access" in System Settings → Battery if you need true reliability.

## Backfill / fresh build

```bash
rm data/warehouse.duckdb     # nuke the warehouse
make extract                  # this is the expensive step — pulls everything from Helius
make full-rebuild             # dbt with --full-refresh; needed after schema/logic changes
make serve
```

After a schema/logic change in stg_inner_ix or stg_token_changes (the
incremental tables), force a full rebuild ONCE:

```bash
make full-rebuild  # cd transform && dbt build --full-refresh
```

This re-processes all 568K txs (~8 min). Subsequent `make refresh` runs go
back to incremental.

## Supply drift / coverage backfill

Run this when C7 warns, C13b fails, or a supply looks wrong. Full background in
`docs/ACCURACY.md` (incident 2026-08-02).

**Diagnose first — do not skip to the backfill.** C7 says a number drifted; it
does not say why. The cause is usually a *signature coverage* gap upstream:

```bash
python -m ops.accuracy_check --deep      # C6 names the mint and the % missing
```

If C6 reports missing txs, the indexer is not seeing them — fix
`watch_addresses()` in `extract_load/extract_signatures.py` before backfilling,
or you will re-derive from the same incomplete data. (`syMint` was missing until
2026-08-02; that single omission put SY 33% low.)

Then, in order:

```bash
# 1. Newly-watched addresses have no scan_state -> full walk to genesis.
python -m extract_load.extract_signatures
python -m extract_load.extract_transactions
cd transform && DBT_PROFILES_DIR=. dbt build && cd ..
python -m ops.accuracy_check --deep       # C6 should now be clean

# 2. Only if history is still wrong (the reconstruction cannot be repaired
#    from tx data alone). Anchors on today's authoritative supply and walks
#    backward through real mint/burn txs -- 60d window, ~1h, run locally.
python -m extract_load.extract_mint_supplies     # fresh anchor FIRST
python -m extract_load.extract_sy_supply_history # DELETEs + rewrites the table
```

Step 1 is the real fix: with complete tx coverage the reconstruction is correct
for *all* history by itself. Step 2 is a workaround that only covers SY, only
inside its window, and only for dates it derives.

**Back up before step 2** — it drops the whole table:

```sql
CREATE OR REPLACE TABLE main.raw_sy_supply_history_bak AS
  SELECT * FROM main.raw_sy_supply_history;
```

**Validate the backfill against an anchor it did not use.** The walk-backward
starts from today, so an authoritative snapshot from a *past* date is an
independent test: derived supply on that date must match `raw_mint_supplies`.
If it doesn't, the derivation has a hole — do not ship it.

Never "backfill" by writing today's supply across past dates. Supply genuinely
moves (USX SY: 54.7M on 07-11, 52.7M on 08-02), so that invents movement, and
it invents it in a shape plausible enough to survive review.

## Adding a new metric

1. Decide which mart it belongs in (`marts/core` for facts, `marts/analytics` for aggregates).
2. Write the SQL file. Reference upstream models with `{{ ref('...') }}`.
3. Add tests in `schema.yml` next to the model (`unique`, `not_null`, custom).
4. Run `cd transform && dbt build --select <new_model>+`. The `+` builds downstream too.
5. Expose via `serve/queries.py` and a new entry in `serve/build_web_data.py`.
6. Frontend: fetch the new file in the relevant component.

No re-extract needed unless the metric requires a field the parser doesn't keep.

## Adding a new market

Markets are auto-discovered every refresh (`extract_load/extract_markets.py` reads
both Exponent API + on-chain `MarketThree` accounts). When a new market appears:

1. `raw_markets` gets a new row on the next `make extract`.
2. `extract_signatures` discovers sigs for the new SY mint (no cursor → full backfill).
3. `extract_transactions` fetches them (anti-join → only new sigs).
4. dbt's next `transform` includes them automatically.

No manual list to update.

## Debugging

| Symptom | Where to look |
|---|---|
| Volume number looks wrong | `fct_swaps` rows → `assert_amm_conservation` test → `int_amm_swaps` SQL |
| Missing market | `raw_markets` → `stg_markets` → `dim_markets` |
| Frontend shows stale data | Check serve/ ran after transform; check `web/public/` timestamps |
| Extract hangs | Helius rate limit — drop EXTRACT_CONCURRENCY in `.env` |
| dbt error on `inner_instructions` parsing | Solana payload shape varies; check the failing tx in `raw_helius_tx` |

## Backup

```bash
duckdb data/warehouse.duckdb "EXPORT DATABASE 'data/raw_backup/$(date +%F)' (FORMAT PARQUET);"
```

Or just copy the `.duckdb` file — it's one file.

---

## Smoke-test baselines (2026-05-18)

First live full-backfill run, recorded so future runs can detect drift.

| Stage | Wall clock | Helius credits | Rows produced |
|---|---|---|---|
| `extract_markets` | 2.7 sec | ~3 | 92 in `raw_markets` (10 api + 82 onchain) |
| `extract_signatures` full backfill | 30 min 15 sec | ~750 | 584,398 in `raw_signatures` |

**Storage**: `data/warehouse.duckdb` after backfill = **236 MB**.

**Sig date range**: 2024-10-24 → 2026-05-18 (18 months of Exponent activity).

**Coverage decomposition** (rows attributed by first-found address):
- Exponent core program: 568,810 sigs (97.3%)
- Exponent CLMM program: 512 sigs (0.1%)
- Per-market address expansion: 15,076 sigs (2.6%) — these were ALT-hidden / inner-ix from the program scans

The per-market scan adds real coverage but the marginal value over a programs-only scan is small (~3%).

### Phase 2d: extract_transactions full firehose (2026-05-18)

First live full backfill of jsonParsed tx payloads.

| Stage | Wall clock | Credits | Outcome |
|---|---|---|---|
| Initial run (newest-first, batch=50) | 3h 30m | ~512K | Crashed at 90% on a 400 Bad Request after retries exhausted |
| Resume (oldest-first, batch=25) | 41 min | ~56K | 99.9% — 3 batches failed with 408 Request Timeout on archive-deep sigs |
| Cleanup pass (batch=5) | 8 sec | ~75 | 100% — smaller batches isolated the stragglers |
| **Total** | ~4h 12m | ~568K | **567,978 / 567,978 (100%)** |

**Lessons learned:**
- Helius free tier rejects JSON-RPC batches > 50 items with 413 Payload Too Large
- 408 Request Timeout on archive-deep txs — added to TRANSIENT_STATUS for retries
- Single bad batch must NOT kill the whole run — wrap in try/except, log + skip + continue
- Smaller batches on retry (25 → 5) isolated transient failures

**Warehouse after backfill**: 30.2 GB
- ~57 KB average per tx payload (uncompressed JSON in DuckDB row)
- Phase 3 (dbt models) operates entirely from this data — no more RPC for any historical metric

**Sample payload structure** (confirmed has everything Phase 3 needs):
```
blockTime, meta, slot, transaction, version
  meta: computeUnitsConsumed, fee, err, innerInstructions,
        logMessages, pre/postBalances, pre/postTokenBalances
```

### Phase 3a: dbt staging layer (2026-05-18)

First materialization of the staging layer against the full backfill.

| Model | Materialization | Rows | Build time |
|---|---|---|---|
| `stg_helius_tx` | view | 567,978 | <1s |
| `stg_markets` | view | 92 | <1s |
| `stg_prices` | view | 0 (no extract yet) | <1s |
| `stg_inner_ix` | **table** | **13,135,634** | 4:09 (and 8:26 in concurrent build) |
| `stg_token_changes` | **table** | **2,903,942** | 3:50 (concurrent) |

**Why tables for stg_inner_ix and stg_token_changes:** UNNESTing JSON arrays across 568K rows is expensive (~4 min). Marts will join against these heavily; cheaper to materialize once than recompute per query.

**Verified data quality:**
- Top 10 mints by delta-count in `stg_token_changes` match Exponent's known SY mints (HvbiURJrV... = fragSOL, 4CEd2syXcV... = USX, 7EtXTvy1NB... = eUSX)
- Top program in `stg_inner_ix`: spl-token (6.1M ix), Exponent core (3.1M), XP1BRLn8 (2.3M — likely orderbook), system (540K)
- Top parsed types: `transfer` (3.76M), `mintTo` (756K), `burn` (629K), `transferChecked` (31K) — the swap-leg primitives are all there
- All 28 dbt tests (16 source + 12 staging) green

**New programs surfaced** that we don't yet have in `EXPONENT_PROGRAMS`:
- `XP1BRLn8eCYSygrd8er5P4GKdzqKbC3DLoSsS5UYVZy` — heavy use, possibly Exponent orderbook
- `XPerenaJPyvnjseLCn7rgzxF` — Perena platform integration
- `XPJitopeUEhMZVF72Cvswnwr` — Jito platform integration
- `XPBookgQTN2p8Yw1C2La35Xk` — another XP* program

Worth investigating before Phase 3b classifies user-intent (we need to know which programs count as "trades" vs "admin").

### Phase 3b/c/d: intermediate + marts (2026-05-18)

Live trading volume against the full data.

| Model | Materialization | Rows | Build time |
|---|---|---|---|
| `int_classified_events` | table | 578,007 | 15s |
| `int_amm_swaps` | table | 232,797 | <1s |
| `fct_swaps` | table | 232,797 | <1s |
| `dim_markets` | table | 92 | <1s |
| `trading_volume_daily` | table | 4,510 | <1s |

**Action classification distribution (578K txs):**
```
tradePt    137,271      (direct PT trades — v1 missed these entirely)
buyYt      102,174
redeemPt    78,892
addLiq      66,819
removeLiq   50,303
sellYt      46,712
admin       34,304
unknown     29,779      (5.1% — mostly non-Exponent CPI'd programs)
merge       28,942
strip        2,488
```

**Trading volume (all-time, underlying-denominated):**
- Top: USX-01JUN26 ≈ $130.7M / 35K trades
- eUSX-01JUN26 ≈ $81.8M / 41K trades
- xSOL-12AUG26: 42,728 SOL / 23K trades
- ONyc-10SEP26: 30,060 ONyc / 23K trades
- Cumulative PT-side volume: $281M / 109K trades
- Cumulative YT-side volume: $12.5M / 124K trades

**Notional methodology:**
`notional_underlying = max(|signer_outflow|, signer_inflow)` per swap action.
Matches v1's `usdNet` (user capital-flow perspective, not flash-loan-inflated gross AMM notional). To swap to true gross notional later, add an `int_amm_swaps_gross` model that walks `stg_inner_ix` for pool-token-account transfers.

### Phase 5: SY-mint symbol resolution (2026-05-18)

The 18.7% gap from Phase 3 was caused by expired markets with no `source='api'`
row — they had a SY mint but no resolvable ticker → underlying mapping.

**Approach (fully on-chain):**
1. `extract_token_metadata.py` calls Helius DAS `getAsset` for every distinct
   mint in `raw_markets.payload` → Metaplex Token Metadata (`name`, `symbol`,
   `decimals`).
2. `extract_token_metadata.py` also snapshots Exponent's `/tokens` endpoint
   into `raw_exponent_tokens` (~63 underlying tokens with mint, symbol, decimals).
3. `stg_resolved_markets` parses `name="Exponent Wrapped <X>"` (or falls back
   to `symbol` for partner tokens like `kUSDC`, `mUSDC`) and cross-references
   against `raw_exponent_tokens` to resolve the underlying mint.
4. `stg_markets` unions API rows + resolved onchain rows, deduped by `market_key`.
5. `int_amm_swaps` joins on `m.underlying_mint IS NOT NULL` (instead of
   `m.source='api'`) — now includes resolved expired markets.

Picker logic in `tx_to_market` CTE prefers api markets (most metadata) over
resolved onchain, then by maturity-closest-to-trade-time. This prevents an
expired market from "winning" an active market's trade.

**Results:**
- 32 unique SY mints, **32/32 have Metaplex metadata** (100%)
- 90 onchain markets → **57/57 previously-UNKNOWN now resolved** with ticker
- 84/90 onchain markets have a resolved underlying_mint
- Trade coverage: **97.4%** (278,676 / 286,157), up from 81.3%
- New markets surfaced with real notional: USD* ($28M cumulative), sHYUSD ($9M),
  USDC+, syrupUSDC, mUSDC, kUSDC, MLP, ALP, kySOL, jlUSDG

**Note on v1's data:** v1's `mint_symbols.json` was **hand-curated**, no code
generated it. v2's on-chain DAS approach is the canonical (and automatable)
source.

**Unmatched trades**: 7,481 of 286,157 (2.6%) still don't produce a notional. These are on **expired markets** that:
- Have a SY mint whose `name` field on-chain doesn't match the `Exponent Wrapped <X>` pattern AND whose `symbol` doesn't match an entry in `/tokens`
- Or didn't have a delta in the resolved underlying mint (unusual swap structure)

This is acceptable for the dashboard — 97.4% coverage is more than enough to render meaningful volume charts.

### Known gaps (followups, not blocking)

1. **CLMM market decoder.** `extract_markets` decodes MarketThree accounts only on the core program (disc `d404847ea9797914`). The CLMM program uses different discriminators (`69f125c8e002fc5a`, `7a68298dd624de25`, `f2f01a0f94bab9cd`) and a different account layout. Result: 7 of 10 currently-active markets exist only via `source='api'` in `raw_markets`. Active coverage is still 100% (API has them); on-chain coverage of expired CLMM markets is missing.
2. **UNKNOWN-prefixed market keys.** 50 expired markets have SY mints the active API doesn't know about. Their `market_key` falls back to `UNKNOWN-{date}`. To resolve, port v1's `mint_symbols.json` lookup or fetch token metadata via Helius DAS `getAsset`.
3. **Per-address sig attribution.** `raw_signatures.signature` is PRIMARY KEY — a sig found by multiple addresses keeps only the first-found `address`. Fine for the current goal (just need the sig set). If per-market attribution becomes important, add a many-to-many `sig_addresses` table.
