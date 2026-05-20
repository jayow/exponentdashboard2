-- Current holder balances derived from transaction history.
--
-- For every (mint, owner) we observed in stg_token_changes, sum delta_ui
-- across all time → current balance. Equivalent to a getProgramAccounts
-- snapshot, but always as-fresh-as-our-tx-extract and free (no extra RPC).
--
-- Validated against a getProgramAccounts snapshot for YT-USX-01JUN26: same
-- 9 holders, same wallets, identical to within float jitter. Works because
-- extract_signatures crawls every PT/YT/LP/SY/pool/vault address per
-- market, so any tx that mutates a Token account for those mints lands in
-- stg_helius_tx (and therefore stg_token_changes).
--
-- A small dust threshold filters out floating-point residue from txs that
-- net to zero but accumulate jitter.
{{ config(materialized='table') }}

with running as (
    select
        mint,
        owner,
        sum(delta_ui) as amount
    from {{ ref('stg_token_changes') }}
    where owner is not null
    group by mint, owner
)
select
    mint,
    owner,
    amount,
    -- Marker date so downstream models that previously consumed
    -- raw_holders.snapshot_date keep the same shape. We use current_date
    -- since the data reflects "as of the latest tx we extracted".
    current_date as snapshot_date
from running
where amount > 1e-9
