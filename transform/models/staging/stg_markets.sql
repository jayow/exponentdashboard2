-- Typed market dim. Unions API and on-chain sources, dedup by market_key.
-- API rows win when both sources have the same market_key (richer metadata).
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
onchain as (
    select
        market_key,
        'onchain'                                              as source,
        payload->>'$.syMint'                                   as sy_mint,
        payload->>'$.vault'                                    as vault,
        cast(null as varchar)                                  as pt_mint,
        cast(null as varchar)                                  as yt_mint,
        cast(null as varchar)                                  as lp_mint,
        cast(null as varchar)                                  as amm_pool,
        cast(null as varchar)                                  as clmm_orderbook,
        cast(null as varchar)                                  as pool,
        cast(null as varchar)                                  as underlying_mint,
        payload->>'$.underlyingTicker'                         as ticker,
        cast(null as int)                                      as underlying_decimals,
        cast(null as varchar)                                  as platform,
        cast(payload->>'$.maturityTs' as bigint)               as maturity_ts,
        cast(payload->>'$.maturityDate' as date)               as maturity_date,
        'expired'                                              as status,
        cast(null as varchar)                                  as interface_type,
        fetched_at
    from {{ source('raw', 'raw_markets') }}
    where source = 'onchain'
)
select * from api
union all
select * from onchain o
where not exists (
    select 1 from api a where a.market_key = o.market_key
)
