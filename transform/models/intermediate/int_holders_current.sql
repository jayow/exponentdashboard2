-- Current holder balances per (mint, owner), as of the latest snapshot.
--
-- Two source paths, unioned:
--
-- 1. PT (regular SPL token, transferable, usually held directly in user
--    wallets): derived from stg_token_changes, with an outflow-signer remap
--    to attribute PDA-custodied PT (e.g. CLMM position PDAs) back to the
--    user wallet that authorized the outflows.
--
-- 2. YT and LP (custom Anchor accounts, custodied by Exponent core):
--    sourced from raw_positions, which decodes YieldTokenPosition and
--    LpPosition accounts directly. The on-chain account stores the user
--    wallet at offset 8 — no remap needed. Vault is the link to a market.
--
-- Why YT/LP need the Anchor path: their SPL "mint" token accounts are all
-- owned by program PDAs (the pool/orderbook vaults). Scanning the YT mint
-- via stg_token_changes only sees the pool's vault, not the underlying user
-- positions. The Anchor account is where the user is recorded.
{{ config(materialized='table') }}

-- ─── PT branch ─────────────────────────────────────────────────────────
with pt_raw_balances as (
    select c.mint, c.owner, sum(c.delta_ui) as amount
    from {{ ref('stg_token_changes') }} c
    join {{ ref('dim_markets') }} m on m.pt_mint = c.mint
    where c.owner is not null
    group by c.mint, c.owner
),
pt_outflow_signers as (
    select
        c.owner,
        h.signer,
        count(distinct c.signature) as n_sigs
    from {{ ref('stg_token_changes') }} c
    join {{ ref('dim_markets') }} m on m.pt_mint = c.mint
    join {{ ref('stg_helius_tx') }} h using (signature)
    where c.owner is not null and c.delta_ui < 0
    group by c.owner, h.signer
),
pt_owner_summary as (
    select
        owner,
        count(distinct signer) as n_outflow_signers,
        arg_max(signer, n_sigs) as top_signer
    from pt_outflow_signers
    group by owner
),
pt_attributed as (
    select
        b.mint,
        case
            when os.n_outflow_signers is null then b.owner
            when os.n_outflow_signers > 3     then null   -- shared pool
            else coalesce(os.top_signer, b.owner)
        end as owner,
        b.amount
    from pt_raw_balances b
    left join pt_owner_summary os on os.owner = b.owner
),
pt_holders as (
    select mint, owner, sum(amount) as amount
    from pt_attributed
    where owner is not null
    group by mint, owner
    having sum(amount) > 1e-9
),

-- ─── YT / LP branch ────────────────────────────────────────────────────
latest_snap as (
    select max(snapshot_date) as d from {{ source('raw', 'raw_positions') }}
),
positions_latest as (
    select p.leg, p.owner, p.vault, p.amount_raw
    from {{ source('raw', 'raw_positions') }} p, latest_snap ls
    where p.snapshot_date = ls.d and p.amount_raw > 0
),
yt_lp_holders as (
    select
        case when p.leg = 'YT' then m.yt_mint else m.lp_mint end as mint,
        p.owner,
        sum(p.amount_raw / power(10.0, coalesce(m.underlying_decimals, 6))) as amount
    from positions_latest p
    join {{ ref('dim_markets') }} m on m.vault = p.vault
    where
        (p.leg = 'YT' and m.yt_mint is not null)
        or (p.leg = 'LP' and m.lp_mint is not null)
    group by 1, 2
    having sum(p.amount_raw) > 0
)

-- ─── Union ─────────────────────────────────────────────────────────────
select mint, owner, amount, current_date as snapshot_date from pt_holders
union all
select mint, owner, amount, current_date as snapshot_date from yt_lp_holders
