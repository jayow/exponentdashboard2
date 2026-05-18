-- Explode inner instructions: one row per (sig, outer_ix_index, inner_ix_index).
-- Phase 3 lands the real implementation; this is a placeholder shape.
{{ config(materialized='view') }}

select
    signature,
    block_time,
    -- TODO Phase 3: UNNEST inner_instructions into (outer_ix_index, inner_ix_index, program_id, parsed_type, info)
    inner_instructions
from {{ ref('stg_helius_tx') }}
where inner_instructions is not null
