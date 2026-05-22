-- Slim per-tx metadata: signature, block_time, signer, fee, err.
--
-- INCREMENTAL TABLE so the data survives across cloud runs after
-- compact_in_place.py NULLs out raw_helius_tx.payload. Heavy JSON
-- columns (account_keys, token_balances, inner_instructions,
-- log_messages) are NOT persisted here — downstream consumers of
-- those fields (stg_token_changes for token_balances, int_classified_events
-- for log_messages) read them DIRECTLY from raw_helius_tx.payload via
-- their own incremental dbt models. That way each NEW tx's heavy JSON
-- gets parsed once (during the run when payload is still present) into
-- its destination table; the original payload can then be discarded.
{{ config(
    materialized='incremental',
    unique_key='signature',
    incremental_strategy='delete+insert'
) }}

select
    signature,
    block_time,
    slot,
    fetched_at,
    -- Fee payer / primary signer is always accountKeys[0].
    payload->>'$.transaction.message.accountKeys[0].pubkey' as signer,
    (payload->>'$.meta.fee')::bigint  as fee_lamports,
    payload->>'$.meta.err'             as err
from {{ source('raw', 'raw_helius_tx') }}
where payload is not null
{% if is_incremental() %}
  and signature not in (select signature from {{ this }})
{% endif %}
