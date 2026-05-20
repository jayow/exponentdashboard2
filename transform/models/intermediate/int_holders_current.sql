-- Current holder balances per (mint, owner), as of the latest snapshot.
--
-- Two source paths, unioned:
--
-- 1. PT (regular SPL token, freely transferable): plain aggregation over
--    stg_token_changes. Owners with non-zero balance are kept as-is —
--    matches v1's getProgramAccounts(PT mint) approach. Some "owners"
--    are PDAs (e.g. CLMM position vaults), but we count them like v1
--    does instead of trying to attribute back to users.
--
-- 2. YT and LP (custom Anchor accounts, custodied by Exponent core):
--    sourced from raw_positions, which decodes YieldTokenPosition and
--    LpPosition accounts directly. The on-chain account stores the user
--    wallet at offset 8 — owner is the user, vault links to the market.
--
-- Why YT/LP need the Anchor path: their SPL mint's token accounts are all
-- owned by program PDAs (the pool/orderbook vaults). Scanning the YT mint
-- via stg_token_changes only sees the pool's vault, not the underlying
-- user positions. The Anchor account is where the user is recorded.
{{ config(materialized='table') }}

-- ─── PT branch (regular SPL token) ─────────────────────────────────────
-- Use raw_holders (extract_holders' getProgramAccounts snapshot) — that's
-- the on-chain truth (matches Solscan). Deriving from stg_token_changes
-- drifts by a handful of accounts because plain SPL transfers between
-- user wallets bypass all the addresses we crawl in extract_signatures,
-- so we see the inflows but not the matching outflows.
with pt_latest_snap as (
    select pt_mint as mint, max(snapshot_date) as snapshot_date
    from {{ source('raw', 'raw_holders') }} h
    join {{ ref('dim_markets') }} m on m.pt_mint = h.mint
    group by pt_mint
),
pt_holders as (
    select h.mint, h.owner, sum(h.amount) as amount
    from {{ source('raw', 'raw_holders') }} h
    join pt_latest_snap ls on ls.mint = h.mint and ls.snapshot_date = h.snapshot_date
    where h.amount > 0
    group by h.mint, h.owner
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
-- YT positions reference the SY vault (m.vault) at offset 40, while LP
-- positions reference the MarketTwo account (m.amm_pool) at the same
-- offset — different field meaning, same byte layout. Join each leg via
-- its own field.
yt_holders_anchor as (
    select
        m.yt_mint as mint,
        p.owner,
        sum(p.amount_raw / power(10.0, coalesce(m.underlying_decimals, 6))) as amount
    from positions_latest p
    join {{ ref('dim_markets') }} m on m.vault = p.vault
    where p.leg = 'YT' and m.yt_mint is not null
    group by 1, 2
    having sum(p.amount_raw) > 0
),
lp_holders_anchor as (
    select
        m.lp_mint as mint,
        p.owner,
        sum(p.amount_raw / power(10.0, coalesce(m.underlying_decimals, 6))) as amount
    from positions_latest p
    join {{ ref('dim_markets') }} m on m.amm_pool = p.vault
    where p.leg = 'LP' and m.lp_mint is not null
    group by 1, 2
    having sum(p.amount_raw) > 0
),
-- CLMM LpPositions: vault field is the CLMM market account (not amm_pool).
-- Resolve clmm_market → pt_mint via raw_clmm_markets, then pt_mint → market
-- via dim_markets. CLMM markets are a different program from the AMM but
-- track per-user LP positions with the same Anchor discriminator (longer
-- struct; balance at offset 104).
latest_clmm_snap as (
    select max(snapshot_date) as d from {{ source('raw', 'raw_clmm_markets') }}
),
clmm_market_pt as (
    select clmm_market, pt_mint
    from {{ source('raw', 'raw_clmm_markets') }} c, latest_clmm_snap ls
    where c.snapshot_date = ls.d
),
lp_holders_clmm as (
    select
        m.lp_mint as mint,
        p.owner,
        sum(p.amount_raw / power(10.0, coalesce(m.underlying_decimals, 6))) as amount
    from positions_latest p
    join clmm_market_pt cm   on cm.clmm_market = p.vault
    join {{ ref('dim_markets') }} m on m.pt_mint = cm.pt_mint
    where p.leg = 'LP_CLMM' and m.lp_mint is not null
    group by 1, 2
    having sum(p.amount_raw) > 0
),
yt_lp_holders as (
    select mint, owner, amount from yt_holders_anchor
    union all
    select mint, owner, amount from lp_holders_anchor
    union all
    select mint, owner, amount from lp_holders_clmm
)

-- ─── Union ─────────────────────────────────────────────────────────────
select mint, owner, amount, current_date as snapshot_date from pt_holders
union all
select mint, owner, amount, current_date as snapshot_date from yt_lp_holders
