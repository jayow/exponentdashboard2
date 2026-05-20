-- Typed market dim. Three layers, unioned and deduped by market_key:
--   1. API rows  (source='api')      — richest, only active markets
--   2. Resolved onchain rows         — onchain MarketThree + DAS metadata → ticker + underlying
--   3. Raw onchain rows (fallback)   — only when resolution failed
{{ config(materialized='view') }}

with api as (
    select
        market_key,
        'api'                                                  as source,
        payload->>'$.syMint'                                   as sy_mint,
        payload->>'$.vault'                                    as vault,
        payload->>'$.ptMint'                                   as pt_mint,
        payload->>'$.ytMint'                                   as yt_mint,
        payload->>'$.lpMint'                                   as lp_mint,
        payload->>'$.ammPool'                                  as amm_pool,
        payload->>'$.clmmOrderbook'                            as clmm_orderbook,
        payload->>'$.pool'                                     as pool,
        payload->>'$.underlying'                               as underlying_mint,
        payload->>'$.underlyingTicker'                         as ticker,
        cast(payload->>'$.underlyingDecimals' as int)          as underlying_decimals,
        payload->>'$.platform'                                 as platform,
        cast(payload->>'$.maturityTs' as bigint)               as maturity_ts,
        cast(payload->>'$.maturityDate' as date)               as maturity_date,
        payload->>'$.marketStatus'                             as status,
        payload->>'$.interfaceType'                            as interface_type,
        fetched_at
    from {{ source('raw', 'raw_markets') }}
    where source = 'api'
),
pt_yt_derived as (
    select
        market_key,
        'pt_yt_derived'                                        as source,
        sy_mint                                                as sy_mint,
        cast(null as varchar)                                  as vault,
        pt_mint,
        yt_mint,
        lp_mint,
        cast(null as varchar)                                  as amm_pool,
        cast(null as varchar)                                  as clmm_orderbook,
        cast(null as varchar)                                  as pool,
        underlying_mint,
        ticker,
        underlying_decimals,
        cast(null as varchar)                                  as platform,
        cast(null as bigint)                                   as maturity_ts,
        maturity_date,
        case when maturity_date >= current_date
             then 'active' else 'expired' end                  as status,
        cast(null as varchar)                                  as interface_type,
        cast(current_timestamp as timestamp)                   as fetched_at
    from {{ ref('stg_pt_yt_markets') }}
),
resolved_onchain as (
    select
        r.market_key                                           as market_key,
        'onchain_resolved'                                     as source,
        r.sy_mint                                              as sy_mint,
        r.vault                                                as vault,
        r.pt_mint                                              as pt_mint,
        cast(null as varchar)                                  as yt_mint,
        r.lp_mint                                              as lp_mint,
        r.amm_pool                                             as amm_pool,
        cast(null as varchar)                                  as clmm_orderbook,
        cast(null as varchar)                                  as pool,
        r.underlying_mint                                      as underlying_mint,
        r.resolved_ticker                                      as ticker,
        r.underlying_decimals                                  as underlying_decimals,
        cast(null as varchar)                                  as platform,
        r.maturity_ts                                          as maturity_ts,
        r.maturity_date                                        as maturity_date,
        case when r.maturity_date >= current_date
             then 'active' else 'expired' end                  as status,
        cast(null as varchar)                                  as interface_type,
        cast(current_timestamp as timestamp)                   as fetched_at
    from {{ ref('stg_resolved_markets') }} r
),
-- Suppress pt_yt_derived rows whose pt_mint is already covered by a
-- higher-priority source under a DIFFERENT market_key. Otherwise we get
-- ticker-renaming duplicates: the same on-chain market_three account
-- surfaces once with the canonical ticker (e.g. syrupUSDC-25SEP25 via
-- on-chain SY-mint Metaplex) and again with the legacy PT-mint Metaplex
-- ticker (syUSDC-25SEP25). Similar pairs: hyloSOL+ vs hySOL+,
-- wfragBTC vs fragBTC, MLP (USDC-USDT) vs MLP.
pt_yt_derived_filtered as (
    select * from pt_yt_derived
    where pt_mint is null
       or pt_mint not in (
            select pt_mint from api              where pt_mint is not null
            union
            select pt_mint from resolved_onchain where pt_mint is not null
       )
),
-- Coalesce across the 3 layers: api > resolved_onchain > pt_yt_derived.
-- For each market_key, take the FIRST non-null value of each column from
-- the highest-priority layer that has it. This way an api active market
-- gets api's pt/yt mints, but an expired market gets sy_mint from
-- resolved_onchain AND pt_mint from pt_yt_derived.
all_layers as (
    select 1 as priority, * from api
    union all
    select 2 as priority, * from resolved_onchain
    union all
    select 3 as priority, * from pt_yt_derived_filtered
),
final as (
    select
        market_key,
        min(priority)                              as min_priority,
        any_value(source order by priority)        as source,
        any_value(sy_mint              order by priority asc) filter (where sy_mint is not null)              as sy_mint,
        any_value(vault                order by priority asc) filter (where vault is not null)                as vault,
        any_value(pt_mint              order by priority asc) filter (where pt_mint is not null)              as pt_mint,
        any_value(yt_mint              order by priority asc) filter (where yt_mint is not null)              as yt_mint,
        any_value(lp_mint              order by priority asc) filter (where lp_mint is not null)              as lp_mint,
        any_value(amm_pool             order by priority asc) filter (where amm_pool is not null)             as amm_pool,
        any_value(clmm_orderbook       order by priority asc) filter (where clmm_orderbook is not null)       as clmm_orderbook,
        any_value(pool                 order by priority asc) filter (where pool is not null)                 as pool,
        any_value(underlying_mint      order by priority asc) filter (where underlying_mint is not null)      as underlying_mint,
        any_value(ticker               order by priority asc) filter (where ticker is not null)               as ticker,
        any_value(underlying_decimals  order by priority asc) filter (where underlying_decimals is not null)  as underlying_decimals,
        any_value(platform             order by priority asc) filter (where platform is not null)             as platform,
        any_value(maturity_ts          order by priority asc) filter (where maturity_ts is not null)          as maturity_ts,
        any_value(maturity_date        order by priority asc) filter (where maturity_date is not null)        as maturity_date,
        any_value(status               order by priority asc) filter (where status is not null)               as status,
        any_value(interface_type       order by priority asc) filter (where interface_type is not null)       as interface_type,
        max(fetched_at)                                                                                       as fetched_at
    from all_layers
    group by market_key
)
select
    market_key, source,
    sy_mint, vault, pt_mint, yt_mint, lp_mint,
    amm_pool, clmm_orderbook, pool,
    underlying_mint, ticker, underlying_decimals, platform,
    maturity_ts, maturity_date, status, interface_type,
    -- Test/calibration markets — deployed on-chain but never went into real
    -- production. Identified manually (low activity / parallel-to-canonical
    -- maturity). Surfaced as a separate flag so downstream views can
    -- filter them out without misclassifying them as expired or hidden.
    market_key in (
        'USX-10JUN26',   -- 35 swaps, $8 total, all in 3-day Jan 2026 window;
                         -- canonical USX June market is USX-01JUN26 ($56M TVL)
        'eUSX-10JUN26'   -- 45 swaps, $8 total, same window;
                         -- canonical is eUSX-01JUN26 ($5.5M TVL)
    ) as is_test,
    fetched_at
from final
