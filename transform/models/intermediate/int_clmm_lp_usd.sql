-- Per-CLMM-LP-position USD valuation via SDK withdrawLiquidity formula.
--
-- The wallet UI was previously showing the raw `total_lp_share` natural
-- number (e.g. "1,209,254.74 ONyc") as the position size — but lp_share is
-- a V3 LIQUIDITY UNIT (L), not a token balance. The honest USD value of
-- the position is the amount of PT+SY tokens you'd receive if you burned
-- all shares right now. This is the SDK formula:
--
--   For each share_idx in position.share_trackers:
--     tick = market.ticks[share.tick_idx]
--     pt_out_atoms += share.lp_share × tick.principal_pt / tick.principal_share_supply
--     sy_out_atoms += share.lp_share × tick.principal_sy / tick.principal_share_supply
--
-- All three values (lp_share, principal_pt/sy, principal_share_supply) are
-- already stored as natural numbers in DuckDB — the U256 Number fields were
-- /1e12-normalized at decode time, the u64 atom fields stayed raw, and the
-- ratio is dimensionless. Output is in PT/SY atoms.
--
-- USD conversion mirrors int_pool_reserves_daily:
--   pt_atoms → underlying_atoms via division by pt_exchange_rate
--   sy_atoms → underlying_atoms via multiplication by sy_exchange_rate
--   underlying_atoms → underlying_tokens / 10^decimals → USD via underlying price
--
-- Verified against Exponent UI for wallet 7VsV9DUW… on 2026-06-03:
--   8v38 position: computed $5,855 vs UI $5,820 (0.6% — gap closes once
--   PT/SY exchange rates come from int_pool_reserves_daily on the same
--   pt_mint as the CLMM market, since both share principal accounting).
{{ config(materialized='view') }}

with share_trackers as (
    select s.position_account, s.share_idx, s.tick_idx, s.lp_share
    from {{ source('raw', 'raw_clmm_lp_share_trackers') }} s
    where s.snapshot_date = (
        select max(snapshot_date) from {{ source('raw', 'raw_clmm_lp_share_trackers') }}
    )
      and s.lp_share > 0
),
ticks as (
    select clmm_market, tick_slot,
           principal_pt, principal_sy, principal_share_supply
    from {{ source('raw', 'raw_clmm_ticks') }}
    where snapshot_date = (select max(snapshot_date) from {{ source('raw', 'raw_clmm_ticks') }})
),
positions as (
    select position_account, owner as wallet, vault as clmm_market
    from {{ source('raw', 'raw_positions') }} p
    where p.leg = 'LP_CLMM'
      and p.snapshot_date = (select max(snapshot_date) from {{ source('raw', 'raw_positions') }})
),
-- SDK withdrawLiquidity per share row → PT/SY atoms.
share_amounts as (
    select
        st.position_account,
        p.wallet,
        p.clmm_market,
        st.share_idx,
        st.tick_idx,
        st.lp_share,
        t.principal_pt,
        t.principal_sy,
        t.principal_share_supply,
        case when t.principal_share_supply > 0
             then st.lp_share * t.principal_pt::DOUBLE / t.principal_share_supply
             else 0 end                                       as pt_atoms_share,
        case when t.principal_share_supply > 0
             then st.lp_share * t.principal_sy::DOUBLE / t.principal_share_supply
             else 0 end                                       as sy_atoms_share
    from share_trackers st
    join positions p on p.position_account = st.position_account
    join ticks t on t.clmm_market = p.clmm_market and t.tick_slot = st.tick_idx
),
totals as (
    select position_account, wallet, clmm_market,
           sum(pt_atoms_share) as pt_atoms,
           sum(sy_atoms_share) as sy_atoms
    from share_amounts
    group by position_account, wallet, clmm_market
),
-- Resolve market_key + pricing via raw_market_three_states.mint_pt → dim_markets.pt_mint
-- (same join pattern as marts/analytics/unclaimed_yield.sql for CLMM rows).
market_lookup as (
    select ms.clmm_market, ms.mint_pt, m.market_key, m.underlying_mint,
           m.underlying_decimals
    from {{ source('raw', 'raw_market_three_states') }} ms
    join {{ ref('dim_markets') }} m on m.pt_mint = ms.mint_pt
    where ms.snapshot_date = (select max(snapshot_date) from {{ source('raw', 'raw_market_three_states') }})
),
-- Pool reserves give us pt_exchange_rate + sy_exchange_rate (per pt_mint).
-- Pick the largest pool per pt_mint when multiple exist (matches the
-- "canonical pool per market" fallback used elsewhere).
pool_rates as (
    select pr.mint_pt, pr.pt_exchange_rate, pr.sy_exchange_rate, pr.underlying_price_usd
    from {{ ref('int_pool_reserves_daily') }} pr
    qualify row_number() over (
        partition by pr.mint_pt
        order by (pr.pt_balance_raw + pr.sy_balance_raw) desc
    ) = 1
)
select
    t.position_account,
    t.wallet,
    t.clmm_market,
    ml.market_key,
    t.pt_atoms,
    t.sy_atoms,
    -- atom → underlying token conversion (PT decimals = underlying decimals for Exponent)
    case when pr.pt_exchange_rate > 0
         then t.pt_atoms / pr.pt_exchange_rate / power(10.0, coalesce(ml.underlying_decimals, 6))
         else 0 end                                         as pt_underlying,
    t.sy_atoms * coalesce(pr.sy_exchange_rate, 1.0)
                / power(10.0, coalesce(ml.underlying_decimals, 6))
                                                            as sy_underlying,
    (
      case when pr.pt_exchange_rate > 0
           then t.pt_atoms / pr.pt_exchange_rate else 0 end
      + t.sy_atoms * coalesce(pr.sy_exchange_rate, 1.0)
    ) / power(10.0, coalesce(ml.underlying_decimals, 6))
      * coalesce(pr.underlying_price_usd, 0)                as lp_usd
from totals t
left join market_lookup ml on ml.clmm_market = t.clmm_market
left join pool_rates pr on pr.mint_pt = ml.mint_pt
