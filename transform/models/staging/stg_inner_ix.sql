-- One row per inner instruction across all txs.
-- Key: (signature, outer_ix_index, inner_ix_index). ~13M rows full backfill.
--
-- INCREMENTAL: only process txs whose signature isn't yet in the target.
-- First run does a full build (8 min); daily refresh picks up only new sigs
-- (~3K/day → ~5 sec).
--
-- To force a full rebuild after schema/logic changes:
--   dbt run --full-refresh --select stg_inner_ix
{{ config(
    materialized='incremental',
    unique_key=['signature', 'outer_ix_index', 'inner_ix_index'],
    incremental_strategy='delete+insert'
) }}

with src as (
    select * from {{ ref('stg_helius_tx') }}
    where inner_instructions is not null
    {% if is_incremental() %}
      -- Process txs from the last day onward. Cheap aggregate (single MAX)
      -- instead of NOT IN against 13M rows. The unique_key delete+insert
      -- strategy idempotently handles any overlap.
      and block_time >= coalesce(
            (select max(block_time) from {{ this }}) - 86400,
            0
          )
    {% endif %}
),
groups as (
    select
        s.signature,
        s.block_time,
        s.slot,
        cast(g.value->>'$.index' as integer)              as outer_ix_index,
        cast(g.value->'$.instructions' as json[])          as instructions_arr
    from src s,
        unnest(cast(s.inner_instructions as json[])) as g(value)
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
