-- Daily TVL series — protocol total + by market + by platform.
-- Phase 3: port from v1/src/build_daily_tvl.py logic.
{{ config(materialized='table') }}

select
    cast(null as date)    as date,
    cast(null as varchar) as market_key,
    cast(null as varchar) as platform,
    cast(null as double)  as tvl_usd
where false
