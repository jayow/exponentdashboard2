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

with mkt_supply_ptyt as (
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
    select market_key, sy_mint, interface_type, ticker, platform, underlying_mint
    from {{ ref('stg_markets') }} where sy_mint is not null
),
-- v2-only markets (e.g. kUSDG-10AUG26, BulkSOL-31OCT26): the XPBook
-- architecture doesn't mint PT/YT — orderbook offers ARE the position
-- representation. They have SY supply but ptyt_supply=0 forever, so without
-- this branch they'd be silently dropped at the mkt_supply ∩ mkt_to_sy join
-- and never appear in tvl_daily. Synthesize a row per (v2_market, date) at
-- 0 ptyt_supply so they survive the join; the final case statement assigns
-- them an even-split share of SY-TVL when their family has no PT/YT siblings.
v2_only_supply as (
    select
        m.market_key,
        m.ticker,
        m.platform,
        m.underlying_mint,
        s.date,
        0.0 as pt_supply,
        0.0 as yt_supply
    from mkt_to_sy m
    join {{ ref('int_sy_tvl_daily') }} s on s.sy_mint = m.sy_mint
    where m.interface_type = 'v2_xpbook'
      and m.market_key not in (select distinct market_key from mkt_supply_ptyt)
),
mkt_supply as (
    select * from mkt_supply_ptyt
    union all
    select * from v2_only_supply
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
    -- Headline TVL: SY-based, attributed by PT+YT supply weight within
    -- the sy_mint family. Markets with zero PT+YT supply get ZERO TVL —
    -- not a divided share. Reason: when the SY hasn't been split yet,
    -- the deposits are "raw SY" and belong to no specific market.
    -- The unattributed SY is still captured at the protocol level via
    -- int_sy_tvl_daily (which the JSON builder uses for protocol total).
    case
        when f.family_ptyt > 0 and m.ptyt_supply > 0 then
            sy.tvl_usd * (m.ptyt_supply / f.family_ptyt)
        when f.family_ptyt = 0 and f.family_markets > 0 then
            -- v2-only family (no PT/YT splits exist in this SY vault). Distribute
            -- SY-TVL evenly across the v2 markets that share this vault. Coarse
            -- approximation; can be refined to v2-book OI weighting later.
            sy.tvl_usd / f.family_markets
        else
            0
    end                                              as tvl_usd,
    case
        when f.family_ptyt > 0 and m.ptyt_supply > 0 then
            sy.sy_supply * (m.ptyt_supply / f.family_ptyt)
        when f.family_ptyt = 0 and f.family_markets > 0 then
            sy.sy_supply / f.family_markets
        else
            0
    end                                              as tvl_underlying,
    -- Principal/active TVL (old method, for comparison)
    m.pt_supply * sy.underlying_price_usd            as principal_tvl_usd,
    m.pt_supply                                      as pt_supply,
    -- v1-style decomposition using market-implied PT/YT prices:
    --   Principal (PT) = PT_supply × PT_price × underlying_USD
    --   Farm (YT)      = YT_supply × YT_price × underlying_USD  (YT_price = 1 - PT_price)
    -- For markets without trades yet, fall back to ratio=1.0 (all value in PT,
    -- zero in YT) — that's the conservative interpretation.
    m.pt_supply * coalesce(ip.pt_price_ratio, 1.0) * sy.underlying_price_usd        as principal_pt_usd,
    -- YT_supply ≡ PT_supply by Pendle construction (PT and YT are co-minted
    -- 1:1 from each split SY). For old/expired markets we may have
    -- pt_supply but no yt_supply indexed (no YT-* Metaplex name decoded),
    -- so use pt_supply here too rather than letting yt go silently to 0.
    m.pt_supply * coalesce(ip.yt_price_ratio, 0.0) * sy.underlying_price_usd        as farm_yt_usd,
    coalesce(ip.pt_price_ratio, 1.0)                 as pt_price_ratio,
    sy.underlying_price_usd                          as underlying_price_usd,
    sy.price_source                                  as price_source
from mkt_supply_sy m
join family_totals f using (sy_mint, date)
join sy_tvl       sy using (sy_mint, date)
left join {{ ref('int_implied_prices_daily') }} ip
    on ip.market_key = m.market_key and ip.date = m.date
where sy.tvl_usd > 0
