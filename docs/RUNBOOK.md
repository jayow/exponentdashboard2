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

### Known gaps (followups, not blocking)

1. **CLMM market decoder.** `extract_markets` decodes MarketThree accounts only on the core program (disc `d404847ea9797914`). The CLMM program uses different discriminators (`69f125c8e002fc5a`, `7a68298dd624de25`, `f2f01a0f94bab9cd`) and a different account layout. Result: 7 of 10 currently-active markets exist only via `source='api'` in `raw_markets`. Active coverage is still 100% (API has them); on-chain coverage of expired CLMM markets is missing.
2. **UNKNOWN-prefixed market keys.** 50 expired markets have SY mints the active API doesn't know about. Their `market_key` falls back to `UNKNOWN-{date}`. To resolve, port v1's `mint_symbols.json` lookup or fetch token metadata via Helius DAS `getAsset`.
3. **Per-address sig attribution.** `raw_signatures.signature` is PRIMARY KEY — a sig found by multiple addresses keeps only the first-found `address`. Fine for the current goal (just need the sig set). If per-market attribution becomes important, add a many-to-many `sig_addresses` table.
