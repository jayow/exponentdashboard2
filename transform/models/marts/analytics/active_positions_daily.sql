-- Active positions per market per day, in UNDERLYING UNITS (not USD).
--
-- Replaces the prior open_interest_daily framing. There is no meaningful
-- "Open Interest" metric for a yield-AMM: PT and YT are two halves of the
-- same SY, so summing their face-value double-counts the same deposit.
-- Instead, surface the per-leg supply counts directly — that's what
-- Pendle and other yield-AMM dashboards do.
--
-- Legs:
--   PT — Principal Token supply (one row per market_key/date)
--   YT — Yield Token supply (one row per market_key/date)
--   LP — LP Token supply (one row per market_key/date, where lp_mint indexed)
--   SY — Standardized Yield supply, DEDUPED to one row per (sy_mint, date)
--        because a single SY mint backs many maturities of the same ticker.
--        For SY, market_key is set to NULL and ticker is the SY family.
--
-- Carries usd value as side column for cross-ticker comparison if needed,
-- but the primary unit is `supply` in underlying token units.
{{ config(materialized='table') }}

with per_market as (
    select
        s.date,
        s.market_key,
        s.ticker,
        s.platform,
        s.leg,
        s.underlying_mint,
        s.underlying_decimals,
        s.supply_ui                              as supply,
        s.supply_ui * p.price_usd                as usd_value,
        p.price_usd                              as underlying_price_usd
    from {{ ref('int_mint_supplies_daily') }} s
    left join {{ ref('stg_prices') }} p
        on p.mint = s.underlying_mint and p.date = s.date
    where s.leg in ('PT', 'YT', 'LP')
      and s.supply_ui > 0
),
sy_deduped as (
    -- SY: one row per (sy_mint, date). Pick any ticker / underlying_mint
    -- from the markets sharing that sy_mint (they're all the same family
    -- by construction).
    select
        s.date,
        cast(null as varchar)                    as market_key,
        max(s.ticker)                            as ticker,
        max(s.platform)                          as platform,
        'SY'                                     as leg,
        max(s.underlying_mint)                   as underlying_mint,
        max(s.underlying_decimals)               as underlying_decimals,
        max(s.supply_ui)                         as supply,
        max(s.supply_ui) * max(p.price_usd)      as usd_value,
        max(p.price_usd)                         as underlying_price_usd
    from {{ ref('int_mint_supplies_daily') }} s
    left join {{ ref('stg_prices') }} p
        on p.mint = s.underlying_mint and p.date = s.date
    where s.leg = 'SY' and s.supply_ui > 0
    group by s.date, s.mint
)
select * from per_market
union all
select * from sy_deduped
