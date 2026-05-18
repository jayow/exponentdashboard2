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
make all          # extract (incremental) → transform (full rebuild) → serve (slim JSONs)
```

Idempotent. Safe to run repeatedly. ~30 min on first full run, ~5 min incremental thereafter.

## Backfill / fresh build

```bash
rm data/warehouse.duckdb     # nuke the warehouse
make extract                  # this is the expensive step — pulls everything from Helius
make transform
make serve
```

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

**Unmatched trades**: 53,430 of 286,157 classified trades (18.7%) didn't produce a notional row — typically expired markets that don't have a `source='api'` entry in `raw_markets` (active API drops them). Logged as a followup.

### Known gaps (followups, not blocking)

1. **CLMM market decoder.** `extract_markets` decodes MarketThree accounts only on the core program (disc `d404847ea9797914`). The CLMM program uses different discriminators (`69f125c8e002fc5a`, `7a68298dd624de25`, `f2f01a0f94bab9cd`) and a different account layout. Result: 7 of 10 currently-active markets exist only via `source='api'` in `raw_markets`. Active coverage is still 100% (API has them); on-chain coverage of expired CLMM markets is missing.
2. **UNKNOWN-prefixed market keys.** 50 expired markets have SY mints the active API doesn't know about. Their `market_key` falls back to `UNKNOWN-{date}`. To resolve, port v1's `mint_symbols.json` lookup or fetch token metadata via Helius DAS `getAsset`.
3. **Per-address sig attribution.** `raw_signatures.signature` is PRIMARY KEY — a sig found by multiple addresses keeps only the first-found `address`. Fine for the current goal (just need the sig set). If per-market attribution becomes important, add a many-to-many `sig_addresses` table.
