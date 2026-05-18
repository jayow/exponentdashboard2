-- Deduped, typed market dim. API rows preferred for fields they cover; onchain
-- fills the gap (expired markets the API drops).
{{ config(materialized='view') }}

with api as (
    select
        market_key,
        payload->>'$.platform'         as platform,
        payload->>'$.underlyingTicker' as ticker,
        payload->>'$.underlying'       as underlying_mint,
        (payload->>'$.underlyingDecimals')::int as underlying_decimals,
        payload->>'$.syMint'           as sy_mint,
        payload->>'$.maturityDate'     as maturity_date,
        payload->>'$.marketStatus'     as status,
        fetched_at
    from {{ source('raw', 'raw_markets') }}
    where source = 'api'
),
onchain as (
    select
        market_key,
        payload->>'$.platform'   as platform,
        payload->>'$.ticker'     as ticker,
        payload->>'$.underlying' as underlying_mint,
        (payload->>'$.underlyingDecimals')::int as underlying_decimals,
        payload->>'$.syMint'     as sy_mint,
        payload->>'$.maturityDate' as maturity_date,
        'expired'                as status,
        fetched_at
    from {{ source('raw', 'raw_markets') }}
    where source = 'onchain'
)
select * from api
union all
select o.* from onchain o
where not exists (select 1 from api a where a.market_key = o.market_key)
