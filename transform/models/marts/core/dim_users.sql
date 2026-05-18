-- Distinct signers across all classified events.
{{ config(materialized='table') }}

select distinct signer as wallet
from {{ ref('int_classified_events') }}
where signer is not null
