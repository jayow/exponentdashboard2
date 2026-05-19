-- DefiLlama-style TVL: one row per (sy_mint, date).
--
-- Mirrors https://github.com/DefiLlama/DefiLlama-Adapters/blob/main/projects/exponent/index.js:
--   amount = SY_supply × SY_exchange_rate
--   tvl_usd = amount × underlying_USD
--
-- SY mints are shared across multiple maturities of the same ticker (one
-- Exponent vault per SY token, many markets per vault). Deduping at the
-- sy_mint level is the only way to avoid 4-6× double-counting per market.
--
-- SY exchange rate is derived empirically from observed deposit txs (see
-- stg_sy_exchange_rates). On Exponent it's ≡ 1.0 across every SY mint; we
-- still flow it through as data so any future rebasing SY is auto-detected.
{{ config(materialized='table') }}

with sy_supply as (
    -- One row per (sy_mint, date). Multiple market_keys share an sy_mint;
    -- their supply_ui rows are identical (same mint), so any() is safe.
    -- Using max() + group by instead of select distinct because float jitter
    -- (1e-9 differences in the cumulative sum) defeats distinct.
    select
        s.mint                                       as sy_mint,
        s.date,
        max(s.supply_ui)                             as sy_supply_ui,
        max(s.underlying_mint)                       as underlying_mint,
        max(s.underlying_decimals)                   as underlying_decimals
    from {{ ref('int_mint_supplies_daily') }} s
    where s.leg = 'SY'
    group by 1, 2
)
select
    s.date,
    s.sy_mint,
    s.underlying_mint,
    s.sy_supply_ui                                   as sy_supply,
    coalesce(r.rate, 1.0)                            as sy_exchange_rate,
    s.sy_supply_ui * coalesce(r.rate, 1.0)           as tvl_underlying,
    s.sy_supply_ui * coalesce(r.rate, 1.0) * p.price_usd as tvl_usd,
    p.price_usd                                      as underlying_price_usd,
    p.source                                         as price_source,
    r.n_events                                       as rate_n_events
from sy_supply s
left join {{ ref('stg_prices') }} p
    on p.mint = s.underlying_mint and p.date = s.date
left join {{ ref('stg_sy_exchange_rates') }} r
    on r.sy_mint = s.sy_mint and r.date = s.date
where s.sy_supply_ui > 0
