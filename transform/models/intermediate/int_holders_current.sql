-- Current holder balances derived from transaction history, attributed
-- to the underlying user wallet (not the on-chain token-account owner).
--
-- For each token account (keyed by SPL `owner` field) we sum delta_ui to
-- get its current balance, then attribute that balance to:
--   - the wallet itself, if it's a normal user wallet (the only signer of
--     outflows from this account is the wallet itself),
--   - the user wallet behind a PDA, if the PDA has ≤3 distinct outflow
--     signers (covers the user + occasional protocol keepers),
--   - nothing, if it's a shared protocol pool/vault (many distinct outflow
--     signers — each user trading through it).
--
-- Why outflows specifically: only the token account's true authority can
-- authorize an outflow (delta < 0). Inflows give noisy signal because
-- any user can transfer INTO any wallet. So restricting the signer count
-- to outflow txs cleanly separates per-user accounts from shared pools.
--
-- Why this matters: Exponent uses program-owned PDAs to custody user PT/YT
-- positions. A user who calls buyYt sees the YT delivered to a CLMM-owned
-- PDA, not their main wallet. Without remapping, our holder list would
-- show that PDA as the "holder" and the user wouldn't find themselves
-- anywhere.
{{ config(materialized='table') }}

with raw_balances as (
    select
        mint,
        owner,
        sum(delta_ui) as amount
    from {{ ref('stg_token_changes') }}
    where owner is not null
    group by mint, owner
),
-- For each token account, who signed the txs that DECREASED its balance?
-- Only an account's authority can authorize an outflow, so this set is
-- exactly {the wallet} for direct holders or {the user(s)} for PDAs.
outflow_signers as (
    select
        c.owner,
        h.signer,
        count(distinct c.signature) as n_sigs
    from {{ ref('stg_token_changes') }} c
    join {{ ref('stg_helius_tx') }} h using (signature)
    where c.owner is not null
      and c.delta_ui < 0
    group by c.owner, h.signer
),
owner_summary as (
    select
        owner,
        count(distinct signer)   as n_outflow_signers,
        arg_max(signer, n_sigs)  as top_signer
    from outflow_signers
    group by owner
),
attributed as (
    select
        b.mint,
        case
            when os.n_outflow_signers is null     then b.owner   -- never had an outflow → assume user wallet, keep
            when os.n_outflow_signers > 3         then null      -- shared protocol pool — exclude
            else coalesce(os.top_signer, b.owner)                -- attribute to the authority
        end as effective_owner,
        b.amount
    from raw_balances b
    left join owner_summary os on os.owner = b.owner
)
select
    mint,
    effective_owner as owner,
    sum(amount)     as amount,
    current_date    as snapshot_date
from attributed
where effective_owner is not null
group by mint, effective_owner
having sum(amount) > 1e-9
