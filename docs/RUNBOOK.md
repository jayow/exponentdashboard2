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
