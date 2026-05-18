-- Distinct signers across all trades.
-- (int_classified_events doesn't carry signer; fct_swaps does indirectly via
-- the tx — for now we use raw_helius_tx's transaction.feePayer.)
{{ config(materialized='table') }}

select distinct
    json_extract_string(payload, '$.transaction.message.accountKeys[0].pubkey') as wallet
from {{ source('raw', 'raw_helius_tx') }}
where wallet is not null
