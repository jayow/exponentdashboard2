# Exponent Dashboard v2

ELT-architected analytics pipeline + dashboard for Exponent Finance (Solana).
v2 of [exponent.hanyon.app](https://exponent.hanyon.app).

## Why v2

v1 was ETL: Helius RPC → Python parse → JSONL → aggregate scripts → frontend JSONs.
Re-deriving any metric required re-fetching raw data. New analytics meant new fetches.

v2 is ELT: Helius RPC → **raw immutable store** → SQL transforms → slim per-tab JSONs.
Raw data is fetched once, kept forever. New metrics are SQL, not RPC.

## Stack

| Layer | Tool |
|---|---|
| Extract + Load | Python (`extract_load/`) |
| Storage | DuckDB single file (`data/warehouse.duckdb`) |
| Transform | dbt-core (`transform/`) |
| Serve | Python → slim JSONs (`serve/`) |
| Frontend | Next.js 14 static export (`web/`) — ported from v1 |
| Orchestration | Makefile (Dagster later if needed) |

## Layout

```
extract_load/   # Python: fetch raw, dump to DuckDB
transform/      # dbt: stg → int → marts (SQL)
serve/          # DuckDB → web/public/*.json
web/            # Next.js frontend
data/           # DuckDB warehouse + backups (gitignored)
tests/          # Python tests + golden txs for accuracy regression
docs/           # ARCHITECTURE, DATA_DICTIONARY, RUNBOOK
```

## Quick start

```bash
cp .env.example .env             # add HELIUS_KEY_1 / HELIUS_KEY_2
make install                      # python deps + dbt deps
make extract                      # pull raw data into DuckDB
make transform                    # run dbt models
make serve                        # build web/public/ JSONs
make web                          # next dev
```

Or the whole pipeline:

```bash
make all
```

See [docs/RUNBOOK.md](docs/RUNBOOK.md) for daily ops and [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) for design.

## Status

Scaffold phase. Not yet pulling data. v1 remains canonical until v2 is byte-identical on `analytics.json`.
