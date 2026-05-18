-- One row per inner instruction across all txs.
-- Key fields:
--   (signature, outer_ix_index, inner_ix_index) — unique
--   program_id      → which program was CPI'd
--   program_name    → SPL parser shorthand if available ('spl-token', 'system')
--   parsed_type     → for parsed ix: 'transfer', 'transferChecked', 'mintTo', 'burn', etc.
--   parsed_info     → full parsed instruction JSON object (mint, source, destination, amount, ...)
--   data_b58        → raw base58 instruction data when not parsed
--   accounts        → JSON array of account addresses (raw form)
--
-- Materialized as a table because (a) downstream marts hit this very often and
-- (b) UNNESTing 568K JSON columns on every query is wasteful. Expect ~5-7M rows.
{{ config(materialized='table') }}

with groups as (
    select
        s.signature,
        s.block_time,
        s.slot,
        cast(g.value->>'$.index' as integer)              as outer_ix_index,
        cast(g.value->'$.instructions' as json[])          as instructions_arr
    from {{ ref('stg_helius_tx') }} s,
        unnest(cast(s.inner_instructions as json[])) as g(value)
    where s.inner_instructions is not null
)
select
    signature,
    block_time,
    slot,
    outer_ix_index,
    row_number() over (
        partition by signature, outer_ix_index order by (null)
    ) - 1                                                  as inner_ix_index,
    i.value->>'$.programId'                                as program_id,
    i.value->>'$.program'                                  as program_name,
    i.value->'$.parsed'->>'$.type'                         as parsed_type,
    i.value->'$.parsed'->'$.info'                          as parsed_info,
    i.value->>'$.data'                                     as data_b58,
    i.value->'$.accounts'                                  as accounts
from groups,
    unnest(instructions_arr) as i(value)
