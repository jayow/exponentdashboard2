# Data Dictionary

Source of truth for every column in the warehouse. Update when you add models.

## Raw layer (extract_load writes here)

### raw_helius_tx
| Column | Type | Notes |
|---|---|---|
| signature | VARCHAR PK | Solana tx signature, base58 |
| block_time | BIGINT | Unix epoch seconds |
| slot | BIGINT | Solana slot number |
| fetched_at | TIMESTAMP | When the extractor wrote this row |
| payload | JSON | Full `getTransaction` response (jsonParsed) |

### raw_signatures
| Column | Type | Notes |
|---|---|---|
| signature | VARCHAR PK | |
| address | VARCHAR | Address that produced this sig via getSignaturesForAddress |
| block_time | BIGINT | |
| slot | BIGINT | |
| err | JSON | Tx error if any, NULL on success |
| fetched_at | TIMESTAMP | |

### raw_markets
| Column | Type | Notes |
|---|---|---|
| market_key | VARCHAR PK | e.g. `fragSOL-15DEC26` |
| source | VARCHAR | `'api'` or `'onchain'` |
| fetched_at | TIMESTAMP | |
| payload | JSON | Source-specific shape; see stg_markets for the projection |

### raw_prices
| Column | Type | Notes |
|---|---|---|
| price_key | VARCHAR | Token symbol or mint (canonicalized in stg) |
| date | DATE | UTC day |
| price_usd | DOUBLE | |
| source | VARCHAR | `'pyth'` / `'birdeye'` / `'coingecko'` |

### raw_holders
| Column | Type | Notes |
|---|---|---|
| snapshot_date | DATE | |
| mint | VARCHAR | PT/YT/LP mint |
| owner | VARCHAR | Wallet |
| amount | DOUBLE | Token amount (UI decimals) |

### raw_tvl_snapshots
| Column | Type | Notes |
|---|---|---|
| snapshot_date | DATE | UTC close-of-day |
| market_key | VARCHAR | |
| slot | BIGINT | Solana slot the snapshot was read at |
| underlying_balance | DOUBLE | Underlying token balance in the market PDA (UI decimals) |

### raw_pool_state
| Column | Type | Notes |
|---|---|---|
| snapshot_date | DATE | |
| market_key | VARCHAR | |
| slot | BIGINT | |
| sy_reserve | DOUBLE | SY token balance in the AMM pool |
| pt_reserve | DOUBLE | PT token balance in the AMM pool |
| lp_supply | DOUBLE | Total LP tokens minted |

### raw_lst_rates
| Column | Type | Notes |
|---|---|---|
| snapshot_date | DATE | |
| lst_mint | VARCHAR | jitoSOL / mSOL / fragSOL / ... |
| base_mint | VARCHAR | Usually wrapped SOL |
| lst_per_base | DOUBLE | 1 base = N LST tokens. Derived from stake pool account on-chain |

## Marts (the contract serve/ reads from)

### fct_swaps — *the* trading volume source
| Column | Type | Notes |
|---|---|---|
| signature | VARCHAR | |
| block_time | BIGINT | |
| date | DATE | UTC |
| market_key | VARCHAR | FK → dim_markets |
| wallet | VARCHAR | |
| side | VARCHAR | `'PT'` or `'YT'` — user intent |
| action | VARCHAR | `buyPt`/`sellPt`/`buyYt`/`sellYt` |
| pool_address | VARCHAR | Which AMM pool was hit |
| notional_underlying | DOUBLE | **The volume number.** AMM pool's underlying flow, one per tx |
| pt_price | DOUBLE | Effective PT price in underlying terms |

### dim_markets
| Column | Type | Notes |
|---|---|---|
| market_key | VARCHAR PK | |
| platform | VARCHAR | |
| ticker | VARCHAR | |
| underlying_mint | VARCHAR | |
| underlying_decimals | INT | |
| sy_mint | VARCHAR | |
| maturity_date | DATE | |
| status | VARCHAR | `'active'` / `'expired'` |

### analytics.trading_volume_daily
| Column | Type | Notes |
|---|---|---|
| date | DATE | |
| market_key | VARCHAR | |
| platform | VARCHAR | |
| ticker | VARCHAR | |
| side | VARCHAR | PT / YT |
| volume_usd | DOUBLE | `Σ notional_underlying × price` |
| trade_count | BIGINT | |

---

## What "trading volume" means here (read this before changing)

Trading volume is the **underlying token amount that flowed in or out of the AMM Pool**, valued in USD at the day's underlying price.

- Counted **once per swap tx**, never twice
- Strip/merge transfers are **not** volume (no counterparty, no price discovery)
- SY mint/redeem flows are **not** trading volume (those live in TVL flow series)
- LP add/remove is **not** trading volume (separate `fct_lp_events`)
- A YT trade and a PT trade with the same notional contribute the same number — `side` is just a label

If you ever want to change this definition: edit `models/intermediate/int_amm_swaps.sql`. Don't paper over downstream.
