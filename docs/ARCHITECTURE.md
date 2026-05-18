# Architecture

## Why ELT, not ETL

v1 (parent project) collapses extraction and transformation into the same Python
scripts. The classified events file (`events.jsonl`) is the only durable artifact
of the parse — raw `getTransaction` payloads are discarded. Consequences:

- Adding a metric that needs a field the v1 parser didn't keep → re-fetch from Helius
- Fixing a parser bug → re-fetch
- Daily refresh re-walks the same raw txs every time

v2 keeps raw payloads forever and does all transformation in SQL.

## Layers

```
┌─ Sources ──────────┐   ┌─ Extract+Load ─┐   ┌─ Warehouse ─────────┐   ┌─ Transform ─────┐   ┌─ Serve ──┐
│ Solana RPC (Helius)│ ─►│ Python         │ ─►│ raw_helius_tx       │ ─►│ stg_*  (views)  │ ─►│ Python   │
│ Pyth on-chain      │   │ async + dual-  │   │ raw_signatures      │   │ int_*  (views)  │   │ duckdb   │
│ Pyth Hermes API    │   │ key, retries   │   │ raw_markets         │   │ fct_*  (tables) │   │ → JSONs  │
│ Stake pool accts   │   │ Idempotent     │   │ raw_holders         │   │ dim_*  (tables) │   │          │
│ Exponent API*      │   │ upserts        │   │ raw_tvl_snapshots   │   │ analytics_*     │   └──────────┘
└────────────────────┘   └────────────────┘   │ raw_pool_state      │   └─────────────────┘         │
* discovery + labels only                     │ raw_lst_rates       │                                ▼
                                              │ raw_prices          │                       web/public/*.json
                                              └─────────────────────┘                       (slim, per-tab)
                                              DuckDB single file
                                              data/warehouse.duckdb
```

## On-chain-first sourcing

All quantitative state is derived from chain. The Exponent API is consulted
**only** for market discovery + human-readable labels.

| What | Source |
|---|---|
| Market universe | Exponent API (labels) + on-chain `MarketThree` decode (truth) |
| TVL per market | `getAccountInfo` on market PDA underlying balance, sampled daily |
| AMM pool state (PT/SY reserves, LP supply) | `getAccountInfo` on pool account, sampled daily |
| Trading volume | `getTransaction` inner instructions — AMM pool's underlying flow |
| Holders | `getProgramAccounts` per PT/YT/LP mint |
| Base-asset USD prices | Pyth Hermes (historical) + Pyth on-chain accounts (live) |
| LST→base exchange rates | Stake pool account decode on-chain |
| LST USD price | `lst_per_base * base_usd` — composed in `stg_prices` |
| Underlyings without a Pyth feed | `price_usd = NULL`; downstream marts skip USD aggregation for those markets |

## Boundary rules

| Layer | Allowed | Forbidden |
|---|---|---|
| **extract_load/** | HTTP, JSON parse, `INSERT INTO raw_*` | Business logic, derived fields, filtering |
| **transform/staging** | Typing, deduping, exploding arrays | Cross-table joins (except sources), business logic |
| **transform/intermediate** | Cross-staging joins, business prep | Final aggregations |
| **transform/marts/core** | Facts + dims, business definitions | Dashboard-shape concerns |
| **transform/marts/analytics** | Pre-aggregated views ready for serve | Defining what a metric *means* (that's core) |
| **serve/** | SELECTs from analytics, JSON shaping | New aggregations |

If you find yourself reaching across a boundary, the layout is wrong — fix the
boundary, don't smuggle.

## Trading volume — the canonical example

| Concept | Lives in |
|---|---|
| What an AMM swap *is* | `int_amm_swaps.sql` (which transfers count) |
| Per-tx notional | `fct_swaps.sql` (one row, one number) |
| Daily total | `analytics/trading_volume_daily.sql` |
| Dashboard payload shape | `serve/build_web_data.py` |

To change the definition of trading volume, you edit **one file**: `int_amm_swaps.sql`.
Everything downstream rebuilds via `dbt build`.

## Idempotency

Every step is rerunnable:

- **extract**: anti-join on signature → only fetches what's missing
- **transform**: dbt is declarative — `dbt build` is always a full rebuild from raw
- **serve**: writes files atomically (tmp + rename)

`make all` is safe to run repeatedly. A daily cron is just `make all` on a schedule.

## When to leave DuckDB

DuckDB handles GB-scale single-machine analytics easily. Migrate when:

- Warehouse exceeds ~50 GB (we'll see ~1 GB after re-index)
- Multiple writers contend on the file (no concurrent writes today)
- You need network query access from non-pipeline machines

Migration target: Postgres or MotherDuck. The dbt models port unchanged because
both speak SQL; only `profiles.yml` and a few syntax quirks change.
