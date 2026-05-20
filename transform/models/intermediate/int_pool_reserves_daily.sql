-- Per-market AMM pool reserves valued in USD — fully on-chain derivation,
-- no Exponent API dependency.
--
-- Pool reserves are held in two SPL token accounts:
--   SY side: token-account authority = raw_pool_state.pool_account (the
--            XP1BRLn8 program account whose offset-8 SY mint matches).
--   PT side: token-account authority = dim_markets.amm_pool (the
--            MarketThree account itself for that market).
--
-- The pool's SY reserves are SHARED across sibling markets (one pool per
-- underlying), so we apportion them per market by PT-supply weight — same
-- attribution model used in tvl_daily.
--
-- liquidity_usd = (sy_share × sy_exchange_rate + pt_reserve × pt_price_ratio)
--                 × underlying_price_usd
--
-- For SY-only pools (no PT/YT split yet), liquidity_usd = full SY value.
{{ config(materialized='table') }}

with latest_pool_snap as (
    select max(snapshot_date) as d from {{ source('raw', 'raw_pool_state') }}
),
pools as (
    select pool_account, sy_mint
    from {{ source('raw', 'raw_pool_state') }} p, latest_pool_snap ls
    where p.snapshot_date = ls.d
),
sy_reserves as (
    -- Total SY balance currently held by the pool authority, per SY mint.
    -- Derived from tx history — sum of delta_ui over all time.
    select
        p.sy_mint,
        sum(c.delta_ui) as sy_reserve
    from pools p
    join {{ ref('stg_token_changes') }} c
        on c.owner = p.pool_account
       and c.mint  = p.sy_mint
    group by p.sy_mint
),
pt_reserves as (
    -- PT balance held by each market's AMM pool (MarketThree).
    select
        m.market_key,
        sum(c.delta_ui) as pt_reserve
    from {{ ref('dim_markets') }} m
    join {{ ref('stg_token_changes') }} c
        on c.owner = m.amm_pool
       and c.mint  = m.pt_mint
    where m.amm_pool is not null and m.pt_mint is not null
    group by m.market_key
),
mkt_ptyt as (
    -- PT+YT supply per market (PT only is fine here since YT ≡ PT supply).
    select s.market_key, dm.sy_mint, s.supply_ui as pt_supply
    from {{ ref('int_mint_supplies_daily') }} s
    join {{ ref('dim_markets') }} dm using (market_key)
    where s.leg = 'PT'
      and s.date = (select max(date) from {{ ref('int_mint_supplies_daily') }})
),
family_supply as (
    -- For each sy_mint family: total PT across sibling markets — used to
    -- weight the SY-reserve apportionment.
    select sy_mint, sum(pt_supply) as family_pt_supply
    from mkt_ptyt
    group by sy_mint
),
latest_sy_rate as (
    select sy_mint, rate
    from {{ ref('stg_sy_exchange_rates') }} r
    where r.date = (select max(date) from {{ ref('stg_sy_exchange_rates') }} r2 where r2.sy_mint = r.sy_mint)
),
latest_underlying_price as (
    -- Latest USD price per underlying mint (Jupiter/Pyth).
    select mint as underlying_mint, price_usd
    from {{ ref('stg_prices') }} p
    where p.date = (select max(date) from {{ ref('stg_prices') }} p2 where p2.mint = p.mint)
),
latest_pt_price as (
    -- Latest market-implied PT price ratio (0..1 of underlying).
    select market_key, pt_price_ratio
    from {{ ref('int_implied_prices_daily') }} ip
    where ip.date = (select max(date) from {{ ref('int_implied_prices_daily') }} ip2 where ip2.market_key = ip.market_key)
)
select
    m.market_key,
    m.ticker,
    m.platform,
    m.sy_mint,
    coalesce(sr.sy_reserve, 0)                                                    as pool_sy_reserve_total,
    coalesce(pr.pt_reserve, 0)                                                    as pool_pt_reserve,
    coalesce(mpt.pt_supply, 0)                                                    as pt_supply,
    coalesce(fs.family_pt_supply, 0)                                              as family_pt_supply,
    -- SY-share apportionment by PT-supply weight; if a family has zero PT yet,
    -- split evenly across known sibling markets.
    case
        when fs.family_pt_supply > 0 and mpt.pt_supply > 0
            then sr.sy_reserve * (mpt.pt_supply / fs.family_pt_supply)
        else 0
    end                                                                            as sy_reserve_share,
    coalesce(r.rate, 1.0)                                                         as sy_exchange_rate,
    coalesce(ip.pt_price_ratio, 1.0)                                              as pt_price_ratio,
    coalesce(p.price_usd, 0)                                                      as underlying_price_usd,
    -- Final per-market liquidity in USD.
    (
      (case
          when fs.family_pt_supply > 0 and mpt.pt_supply > 0
              then sr.sy_reserve * (mpt.pt_supply / fs.family_pt_supply)
          else 0
       end) * coalesce(r.rate, 1.0)
      + coalesce(pr.pt_reserve, 0) * coalesce(ip.pt_price_ratio, 1.0)
    ) * coalesce(p.price_usd, 0)                                                  as liquidity_usd
from {{ ref('dim_markets') }} m
left join mkt_ptyt mpt                       using (market_key, sy_mint)
left join pt_reserves pr                     using (market_key)
left join sy_reserves sr                     using (sy_mint)
left join family_supply fs                   using (sy_mint)
left join latest_sy_rate r                   using (sy_mint)
left join latest_underlying_price p          on p.underlying_mint = m.underlying_mint
left join latest_pt_price ip                 on ip.market_key     = m.market_key
where m.sy_mint is not null
