-- Daily TVL per market in USD — DefiLlama-compatible methodology.
--
-- Headline TVL = SY_supply × underlying_USD (captures all vault capital:
-- unsplit SY, LP-held SY, and PT-backed SY combined). Mirrors
-- DefiLlama's exponent adapter.
--
-- SY mints are shared across multiple maturities of the same ticker, so we
-- attribute SY-TVL across the markets sharing an sy_mint by their
-- (PT+YT)_supply weight. Markets with zero PT+YT on a date receive 0
-- attribution (their share goes to active siblings). For dates where the
-- entire SY family has zero PT+YT (very early lifecycle), TVL is
-- distributed evenly across known markets of that family.
--
-- principal_tvl_usd = PT_supply × underlying_USD (the old definition,
-- aka "active market size" / Exponent's totalMarketSize field). Kept as a
-- side column for comparison.
{{ config(materialized='table') }}

with mkt_supply as (
    -- PT and YT supply per market per date (and underlying mint)
    select
        market_key, ticker, platform, underlying_mint, date,
        max(case when leg = 'PT' then supply_ui else 0 end) as pt_supply,
        max(case when leg = 'YT' then supply_ui else 0 end) as yt_supply
    from {{ ref('int_mint_supplies_daily') }}
    where leg in ('PT', 'YT')
    group by 1, 2, 3, 4, 5
),
mkt_to_sy as (
    -- Each market_key → its sy_mint
    select market_key, sy_mint from {{ ref('stg_markets') }} where sy_mint is not null
),
mkt_supply_sy as (
    select s.*, m.sy_mint, s.pt_supply + s.yt_supply as ptyt_supply
    from mkt_supply s
    join mkt_to_sy m using (market_key)
),
family_totals as (
    -- For each (sy_mint, date): sum of (PT+YT) across all markets in family,
    -- and total number of markets in family that exist on this date
    select sy_mint, date,
           sum(ptyt_supply) as family_ptyt,
           count(*)         as family_markets
    from mkt_supply_sy
    group by 1, 2
),
sy_tvl as (
    select date, sy_mint, tvl_usd, sy_supply, underlying_price_usd, price_source
    from {{ ref('int_sy_tvl_daily') }}
)
select
    m.date,
    m.market_key,
    m.ticker,
    m.platform,
    m.underlying_mint,
    -- Headline TVL: SY-based, attributed to market
    case
        when f.family_ptyt > 0 then
            sy.tvl_usd * (m.ptyt_supply / f.family_ptyt)
        else
            sy.tvl_usd / nullif(f.family_markets, 0)
    end                                              as tvl_usd,
    case
        when f.family_ptyt > 0 then
            sy.sy_supply * (m.ptyt_supply / f.family_ptyt)
        else
            sy.sy_supply / nullif(f.family_markets, 0)
    end                                              as tvl_underlying,
    -- Principal/active TVL (old method, for comparison)
    m.pt_supply * sy.underlying_price_usd            as principal_tvl_usd,
    m.pt_supply                                      as pt_supply,
    sy.underlying_price_usd                          as underlying_price_usd,
    sy.price_source                                  as price_source
from mkt_supply_sy m
join family_totals f using (sy_mint, date)
join sy_tvl       sy using (sy_mint, date)
where sy.tvl_usd > 0
