-- Resolve which market a tx touched. A tx may touch multiple SY mints (rare),
-- so this can fan out — fct_swaps then attributes notional per market.
{{ config(materialized='view') }}

select
    signature,
    cast(null as varchar) as market_key
from {{ ref('stg_helius_tx') }}
where false  -- Phase 3
