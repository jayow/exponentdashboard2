-- Per-tx, per-(account, mint) token balance deltas derived from pre/post balances.
-- Phase 3 implements; this is a shape placeholder.
{{ config(materialized='view') }}

select
    signature,
    block_time,
    pre_token_balances,
    post_token_balances
from {{ ref('stg_helius_tx') }}
