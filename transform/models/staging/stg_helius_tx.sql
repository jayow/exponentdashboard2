-- Typed projection of raw_helius_tx. No business logic.
-- Lifts hot fields out of payload JSON; inner instructions explode in stg_inner_ix.
{{ config(materialized='view') }}

select
    signature,
    block_time,
    slot,
    fetched_at,
    payload,
    payload->'$.transaction.message.accountKeys' as account_keys,
    payload->'$.meta.preTokenBalances'           as pre_token_balances,
    payload->'$.meta.postTokenBalances'          as post_token_balances,
    payload->'$.meta.innerInstructions'          as inner_instructions,
    payload->'$.meta.logMessages'                as log_messages,
    (payload->>'$.meta.fee')::bigint             as fee_lamports,
    payload->>'$.meta.err'                        as err
from {{ source('raw', 'raw_helius_tx') }}
