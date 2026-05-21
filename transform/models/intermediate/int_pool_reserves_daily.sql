-- Per-pool AMM TVL ("Liquidity"), matching Exponent's UI exactly.
--
-- Pendle-style: liquidityPoolTvl_underlying
--   = sy_balance × sy_exchange_rate + pt_balance / pt_exchange_rate
-- where pt_exchange_rate = exp(last_ln_implied_rate × years_to_maturity).
--
-- KEY CHANGE from earlier versions: this model is now keyed by
-- `pool_account` (the MarketTwo address), NOT by market_key. Several
-- markets have multiple MarketTwo accounts on-chain — a deprecated v1 pool
-- and a live v2 pool, both sharing the same (pt_mint, sy_mint, vault).
-- LP positions reference one specific pool_account via their .vault field,
-- so per-pool reserves are needed to value LP holdings correctly.
--
-- Aggregation back to one row per market_key is in
-- int_pool_reserves_daily_canonical (uses API-canonical amm_pool, falls back
-- to the largest pool by reserves).
{{ config(materialized='table') }}

with latest_snap as (
    select max(snapshot_date) as snapshot_date from {{ source('raw', 'raw_market_two_pools') }}
),
pools as (
    select * from {{ source('raw', 'raw_market_two_pools') }}
    where snapshot_date = (select snapshot_date from latest_snap)
),
latest_sy_rate as (
    select sy_mint, rate
    from {{ ref('stg_sy_exchange_rates') }} r
    where r.date = (
        select max(date) from {{ ref('stg_sy_exchange_rates') }} r2
        where r2.sy_mint = r.sy_mint
    )
),
latest_underlying_price as (
    select mint as underlying_mint, price_usd
    from {{ ref('stg_prices') }} p
    where p.date = (
        select max(date) from {{ ref('stg_prices') }} p2 where p2.mint = p.mint
    )
),
-- Join each pool to its market via pt_mint (deduplicates the dim_markets
-- match — multiple pools per market share pt_mint).
pool_market as (
    select p.*, m.market_key, m.ticker, m.platform,
           m.underlying_mint, m.underlying_decimals
    from pools p
    left join {{ ref('dim_markets') }} m on m.pt_mint = p.mint_pt
)
select
    pm.pool_account,
    pm.market_key,
    pm.ticker,
    pm.platform,
    pm.mint_pt,
    pm.mint_sy,
    pm.mint_lp,
    pm.vault                                  as sy_vault,
    pm.underlying_mint,
    pm.pt_balance_raw,
    pm.sy_balance_raw,
    pm.last_ln_implied_rate,
    coalesce(r.rate, 1.0)                     as sy_exchange_rate,
    coalesce(p.price_usd, 0)                  as underlying_price_usd,
    greatest(0.0, (pm.expiration_ts - extract('epoch' from current_timestamp))) / 31536000.0
                                              as years_remaining,
    exp(
        pm.last_ln_implied_rate
        * greatest(0.0, (pm.expiration_ts - extract('epoch' from current_timestamp))) / 31536000.0
    )                                          as pt_exchange_rate,
    (
      pm.sy_balance_raw * coalesce(r.rate, 1.0)
      + pm.pt_balance_raw / exp(
            pm.last_ln_implied_rate
            * greatest(0.0, (pm.expiration_ts - extract('epoch' from current_timestamp))) / 31536000.0
        )
    ) / power(10.0, coalesce(pm.underlying_decimals, 6))
                                              as liquidity_underlying,
    (
      pm.sy_balance_raw * coalesce(r.rate, 1.0)
      + pm.pt_balance_raw / exp(
            pm.last_ln_implied_rate
            * greatest(0.0, (pm.expiration_ts - extract('epoch' from current_timestamp))) / 31536000.0
        )
    ) / power(10.0, coalesce(pm.underlying_decimals, 6))
      * coalesce(p.price_usd, 0)               as liquidity_usd
from pool_market pm
left join latest_sy_rate r       on r.sy_mint = pm.mint_sy
left join latest_underlying_price p on p.underlying_mint = pm.underlying_mint
