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
        cast(null as varchar)                                  as sy_mint,
        cast(null as varchar)                                  as vault,
        pt_mint,
        yt_mint,
        cast(null as varchar)                                  as lp_mint,
        cast(null as varchar)                                  as amm_pool,
        cast(null as varchar)                                  as clmm_orderbook,
        cast(null as varchar)                                  as pool,
        underlying_mint,
        ticker,
        underlying_decimals,
        cast(null as varchar)                                  as platform,
        cast(null as bigint)                                   as maturity_ts,
        maturity_date,
        'expired'                                              as status,
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
        cast(null as varchar)                                  as pt_mint,
        cast(null as varchar)                                  as yt_mint,
        cast(null as varchar)                                  as lp_mint,
        cast(null as varchar)                                  as amm_pool,
        cast(null as varchar)                                  as clmm_orderbook,
        cast(null as varchar)                                  as pool,
        r.underlying_mint                                      as underlying_mint,
        r.resolved_ticker                                      as ticker,
        r.underlying_decimals                                  as underlying_decimals,
        cast(null as varchar)                                  as platform,
        r.maturity_ts                                          as maturity_ts,
        r.maturity_date                                        as maturity_date,
        'expired'                                              as status,
        cast(null as varchar)                                  as interface_type,
        cast(current_timestamp as timestamp)                   as fetched_at
    from {{ ref('stg_resolved_markets') }} r
),
-- Priority: api > resolved_onchain > pt_yt_derived
final as (
    select * from api
    union all
    select r.* from resolved_onchain r
    where not exists (select 1 from api a where a.market_key = r.market_key)
    union all
    select d.* from pt_yt_derived d
    where not exists (select 1 from api a where a.market_key = d.market_key)
      and not exists (select 1 from resolved_onchain r where r.market_key = d.market_key)
)
select * from final
