-- Per-wallet roll-up combining current holdings, lifetime swap activity,
-- claims, and unclaimed yield. One row per wallet.
--
-- Fields:
--   total_holdings_usd  — sum of PT+YT+LP USD in active markets
--   pt_usd / yt_usd / lp_usd — leg breakdown
--   claim_usd           — total USD of claimYield events for this signer
--   unclaimed_usd       — current YT USD if signer has NEVER claimed; else 0
--   n_swaps, n_markets  — lifetime totals from int_amm_swaps
--   buy_yt / sell_yt / buy_pt / sell_pt — lifetime action counts
--   first_seen / last_seen — earliest/latest swap day
--   active_holdings_markets — count of (market, leg) currently held
--
-- Powers the "Users" leaderboard, cohort buckets, and retention views.
{{ config(materialized='table') }}

with wallet_holdings as (
    select
        wallet,
        sum(usd_value)                              as total_holdings_usd,
        sum(usd_value) filter (where leg = 'PT')    as pt_usd,
        sum(usd_value) filter (where leg = 'YT')    as yt_usd,
        sum(usd_value) filter (where leg = 'LP')    as lp_usd,
        count(distinct market_key)                  as active_markets_held,
        count(*)                                    as active_positions
    from {{ ref('int_wallet_holdings_usd') }}
    group by wallet
),
-- Real unclaimed = staged interest (already moved to position buffer) +
-- unstaged accrued interest (still in the vault, computed via
-- yield_token_position.rs::calc_earned_sy from position.lastSeenIndex +
-- vault.final_sy_exchange_rate). Both pieces come from the canonical
-- unclaimed_yield mart — sum per wallet to get total USD exposure.
-- Market emissions (USDC/SOL paid to YT/LP holders) is a separate
-- unclaimed source we don't decode yet.
unclaimed_yt as (
    select wallet, sum(unclaimed_usd) as unclaimed_usd
    from {{ ref('unclaimed_yield') }}
    group by wallet
),
-- claimYield events: int_classified_events doesn't carry signer, so we
-- join through stg_helius_tx to attribute. Only ~10 events globally —
-- the classifier doesn't catch CollectInterest reliably yet — so this
-- is sparse. notional_usd isn't decoded; we just count claims.
claims as (
    select
        t.signer                                               as wallet,
        count(*)                                               as n_claims
    from {{ ref('int_classified_events') }} c
    join {{ ref('stg_helius_tx') }} t using (signature)
    where c.action = 'claimYield' and t.signer is not null
    group by t.signer
),
swaps as (
    select * from {{ ref('user_lifetime_stats') }}
),
all_wallets as (
    -- union of holders + swappers + claimers + staged-unclaimed
    select wallet from wallet_holdings
    union
    select signer as wallet from swaps where signer is not null
    union
    select wallet from claims
    union
    select wallet from unclaimed_yt
)
select
    a.wallet,
    coalesce(h.total_holdings_usd, 0)                as total_holdings_usd,
    coalesce(h.pt_usd, 0)                            as pt_usd,
    coalesce(h.yt_usd, 0)                            as yt_usd,
    coalesce(h.lp_usd, 0)                            as lp_usd,
    coalesce(h.active_markets_held, 0)               as active_markets_held,
    coalesce(h.active_positions, 0)                  as active_positions,
    coalesce(c.n_claims, 0)                          as n_claims,
    -- Unclaimed = staged + unstaged YT interest, valued in underlying USD.
    -- Sourced from main_analytics.unclaimed_yield which applies the canonical
    -- Pendle/Exponent formula floor((1/lsi − 1/final_sy_rate) × yt_balance).
    -- Market emissions (USDC/SOL rewards distributed to YT/LP holders) is
    -- a separate unclaimed source we don't decode yet — small in practice
    -- relative to SY-side accrual on LST-backed markets.
    coalesce(uy.unclaimed_usd, 0)                    as unclaimed_usd,
    coalesce(s.n_swaps, 0)                           as n_swaps,
    coalesce(s.total_volume_usd, 0)                  as total_volume_usd,
    coalesce(s.n_markets, 0)                         as lifetime_markets,
    coalesce(s.n_buy_yt, 0)                          as n_buy_yt,
    coalesce(s.n_sell_yt, 0)                         as n_sell_yt,
    coalesce(s.n_buy_pt, 0)                          as n_buy_pt,
    coalesce(s.n_sell_pt, 0)                         as n_sell_pt,
    s.first_seen,
    s.last_seen,
    coalesce(s.active_span_days, 0)                  as active_span_days,
    -- Heuristic: wallets that hold PT/YT/LP but never signed a swap are
    -- almost always program PDAs — CLMM position vaults, AMM reserves,
    -- MarketTwo internal accounts. Token authority is the PDA, not the
    -- user who deposited. v1 maintained a hand-curated list; this is the
    -- conservative inverse: "holds without ever swapping" catches every
    -- PDA we've seen, with some false positives (genuine HODL wallets
    -- that received PT via transfer). UI can hide these by default.
    case when coalesce(h.total_holdings_usd, 0) > 0
              and coalesce(s.n_swaps, 0) = 0
              and coalesce(s.n_markets, 0) = 0
         then true else false end                     as is_likely_pool
from all_wallets a
left join wallet_holdings h on h.wallet = a.wallet
left join swaps           s on s.signer = a.wallet
left join claims          c on c.wallet = a.wallet
left join unclaimed_yt    uy on uy.wallet = a.wallet
