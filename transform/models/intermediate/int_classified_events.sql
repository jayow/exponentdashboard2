-- Classify each Exponent tx by user-intent action: buyYt, sellYt, addLiq, removeLiq,
-- claimYield, strip, redeemPt, other. Driven off log messages + invoke depth.
--
-- Phase 3: port the classification table from v1/src/classify_events.py (the
-- instr-name → action map) and lift to SQL.
{{ config(materialized='view') }}

select
    signature,
    block_time,
    cast(null as varchar) as market_key,
    cast(null as varchar) as signer,
    cast(null as varchar) as instr,
    cast(null as varchar) as action
from {{ ref('stg_helius_tx') }}
where false  -- Phase 3
