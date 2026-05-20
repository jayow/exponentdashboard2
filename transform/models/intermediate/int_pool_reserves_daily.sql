-- Per-market AMM pool TVL ("Liquidity"), matching Exponent's UI exactly.
--
-- Formula straight from the protocol's own SDK (market.js):
--   liquidityPoolTvl = sy_balance × sy_exchange_rate
--                    + pt_balance / pt_exchange_rate
-- where pt_exchange_rate = exp(last_ln_implied_rate × years_to_maturity),
-- the time-curve rate from exponent_time_curve::math::exchange_rate_from_ln_implied_rate.
--
-- sy_balance and pt_balance are the AMM's own internally-tracked balances,
-- stored inside the MarketTwo account at offsets 380 and 372 respectively.
-- These DIFFER from total token-account holdings (the vault holds all
-- protocol SY, not just the pool's reserves) — we read them from the
-- raw_markets `onchain` payload.
--
-- Validated against Exponent's API legacyLiquidity for USX-01JUN26:
-- derived 26,113,409.93 USX vs API 26,113,405.76 USX → 4 USX drift across
-- slot timing. Same exact formula.
{{ config(materialized='table') }}

with onchain_payloads as (
    select
        market_key,
        cast(json_extract_string(cast(payload as json), '$.ptBalance')         as ubigint) as pt_balance_raw,
        cast(json_extract_string(cast(payload as json), '$.syBalance')         as ubigint) as sy_balance_raw,
        cast(json_extract_string(cast(payload as json), '$.lastLnImpliedRate') as double)  as last_ln_implied_rate,
        cast(json_extract_string(cast(payload as json), '$.maturityTs')        as bigint)  as maturity_ts
    from {{ source('raw', 'raw_markets') }}
    where source = 'onchain'
      and json_extract_string(cast(payload as json), '$.ptBalance')         is not null
      and json_extract_string(cast(payload as json), '$.syBalance')         is not null
      and json_extract_string(cast(payload as json), '$.lastLnImpliedRate') is not null
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
)
select
    m.market_key,
    m.ticker,
    m.platform,
    m.sy_mint,
    m.underlying_mint,
    o.pt_balance_raw,
    o.sy_balance_raw,
    o.last_ln_implied_rate,
    coalesce(r.rate, 1.0)        as sy_exchange_rate,
    coalesce(p.price_usd, 0)     as underlying_price_usd,
    -- Time-to-maturity in years (clamped at 0 — expired markets get rate=1).
    greatest(0.0, (o.maturity_ts - extract('epoch' from current_timestamp))) / 31536000.0
                                  as years_remaining,
    -- pt_exchange_rate = e^(last_ln_implied × years_remaining)
    exp(
        o.last_ln_implied_rate
        * greatest(0.0, (o.maturity_ts - extract('epoch' from current_timestamp))) / 31536000.0
    )                              as pt_exchange_rate,
    -- liquidity in raw underlying atom units
    (
      o.sy_balance_raw * coalesce(r.rate, 1.0)
      + o.pt_balance_raw / exp(
            o.last_ln_implied_rate
            * greatest(0.0, (o.maturity_ts - extract('epoch' from current_timestamp))) / 31536000.0
        )
    ) / power(10.0, coalesce(m.underlying_decimals, 6))
                                  as liquidity_underlying,
    -- liquidity in USD via Jupiter/Pyth price
    (
      o.sy_balance_raw * coalesce(r.rate, 1.0)
      + o.pt_balance_raw / exp(
            o.last_ln_implied_rate
            * greatest(0.0, (o.maturity_ts - extract('epoch' from current_timestamp))) / 31536000.0
        )
    ) / power(10.0, coalesce(m.underlying_decimals, 6))
      * coalesce(p.price_usd, 0)  as liquidity_usd
from {{ ref('dim_markets') }} m
join onchain_payloads o          using (market_key)
left join latest_sy_rate r       using (sy_mint)
left join latest_underlying_price p on p.underlying_mint = m.underlying_mint
