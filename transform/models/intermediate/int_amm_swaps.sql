-- Identify AMM pool transfers per tx. The "trading volume" comes from here:
-- one row per tx where the AMM pool sent or received underlying.
--
-- Phase 3: join stg_inner_ix to dim_markets (pool addresses) and filter to
-- Transfer ix where source or destination is a known AMM pool account.
{{ config(materialized='view') }}

select
    signature,
    block_time,
    cast(null as varchar)  as market_key,
    cast(null as varchar)  as pool_address,
    cast(null as double)   as underlying_in,    -- AMM received underlying
    cast(null as double)   as underlying_out,   -- AMM sent underlying
    cast(null as double)   as pt_in,
    cast(null as double)   as pt_out,
    cast(null as double)   as pt_price
from {{ ref('stg_inner_ix') }}
where false  -- Phase 3 lights this up
