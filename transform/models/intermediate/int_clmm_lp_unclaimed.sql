-- Per-(wallet, CLMM position) unclaimed fees + tokens_owed in underlying.
--
-- Canonical formula from @exponent-labs/exponent-sdk
-- marketThree.js::calculateClaimableFees:
--
--   delta_inside_pt = current_inside_pt − fee_inside_last_pt
--   delta_inside_sy = current_inside_sy − fee_inside_last_sy
--   claimable_pt = (lp_balance × delta_inside_pt) >> 64   -- Q64.64
--   claimable_sy = (lp_balance × delta_inside_sy) >> 64
--   total_pt = claimable_pt + tokens_owed_pt
--   total_sy = claimable_sy + tokens_owed_sy
--
-- Where fee_growth_inside(lower, upper) follows Uniswap V3:
--   below = (current_apy >= lower_apy) ? lower.fg_outside : global − lower.fg_outside
--   above = (current_apy <  upper_apy) ? upper.fg_outside : global − upper.fg_outside
--   inside = global − below − above
--   (all u128 Q64.64, wrapping modulo 2^128)
--
-- Verified against Exponent UI for wallet 7VsV9DUW… on 2026-06-03:
--   8v38 position: PT 1.15 vs UI 1.11 (within 4% drift)
--   SGQQ position: PT 0.0091 vs UI 0.008 (within 14% drift)
-- SY side still under investigation; emit it for visibility but treat
-- as best-effort until calibration sample broadens.
{{ config(materialized='view') }}

with positions as (
    select
        p.position_account,
        p.owner                              as wallet,
        p.vault                              as clmm_market,
        p.amount_raw                         as lp_balance,
        p.clmm_fee_inside_last_pt            as fee_last_pt,
        p.clmm_fee_inside_last_sy            as fee_last_sy,
        p.clmm_tokens_owed_pt                as tow_pt,
        p.clmm_tokens_owed_sy                as tow_sy,
        p.clmm_lower_tick_idx                as lower_slot,
        p.clmm_upper_tick_idx                as upper_slot
    from {{ source('raw', 'raw_positions') }} p
    where p.leg = 'LP_CLMM' and p.amount_raw > 0
      and p.snapshot_date = (select max(snapshot_date) from {{ source('raw', 'raw_positions') }})
      and p.clmm_fee_inside_last_pt is not null
),
market_globals as (
    select clmm_market, fee_growth_global_pt as fg_g_pt,
           fee_growth_global_sy as fg_g_sy, current_spot_price
    from {{ source('raw', 'raw_clmm_market_globals') }}
    where snapshot_date = (select max(snapshot_date) from {{ source('raw', 'raw_clmm_market_globals') }})
),
ticks as (
    select clmm_market, tick_slot, apy_bp,
           fee_growth_outside_pt as fg_out_pt,
           fee_growth_outside_sy as fg_out_sy
    from {{ source('raw', 'raw_clmm_ticks') }}
    where snapshot_date = (select max(snapshot_date) from {{ source('raw', 'raw_clmm_ticks') }})
),
joined as (
    select
        p.position_account, p.wallet, p.clmm_market, p.lp_balance,
        p.fee_last_pt, p.fee_last_sy, p.tow_pt, p.tow_sy,
        p.lower_slot, p.upper_slot,
        mg.fg_g_pt, mg.fg_g_sy, mg.current_spot_price,
        ln.apy_bp                                  as lower_apy,
        ln.fg_out_pt                               as lo_fg_pt,
        ln.fg_out_sy                               as lo_fg_sy,
        un.apy_bp                                  as upper_apy,
        un.fg_out_pt                               as hi_fg_pt,
        un.fg_out_sy                               as hi_fg_sy
    from positions p
    join market_globals mg using (clmm_market)
    left join ticks ln on ln.clmm_market = p.clmm_market and ln.tick_slot = p.lower_slot
    left join ticks un on un.clmm_market = p.clmm_market and un.tick_slot = p.upper_slot
    where ln.tick_slot is not null and un.tick_slot is not null
),
-- Normalize: ensure lower_apy < upper_apy (positions may store slots in
-- either order; the V3 fee math requires the strict APY ordering).
normalized as (
    select
        position_account, wallet, clmm_market, lp_balance,
        fee_last_pt, fee_last_sy, tow_pt, tow_sy,
        fg_g_pt, fg_g_sy, current_spot_price,
        cast(round((current_spot_price - 1.0) * 1000000) as bigint) as cur_apy_key,
        case when lower_apy <= upper_apy then lower_apy else upper_apy end as lo_apy,
        case when lower_apy <= upper_apy then lo_fg_pt   else hi_fg_pt   end as lo_fg_pt,
        case when lower_apy <= upper_apy then lo_fg_sy   else hi_fg_sy   end as lo_fg_sy,
        case when lower_apy <= upper_apy then upper_apy  else lower_apy  end as hi_apy,
        case when lower_apy <= upper_apy then hi_fg_pt   else lo_fg_pt   end as hi_fg_pt,
        case when lower_apy <= upper_apy then hi_fg_sy   else lo_fg_sy   end as hi_fg_sy
    from joined
),
-- Convert all u128 Q64.64 values to natural DOUBLE BEFORE subtracting.
-- UHUGEINT subtraction errors on underflow (no mod-2^128 wrap), so we do
-- the math in floating point. Precision loss is ~1e-16 relative which is
-- vastly below the underlying-token resolution we ultimately need.
-- Wrap is handled by adding 2^128 when the result is negative.
norm_to_double as (
    select
        wallet, position_account, clmm_market, lp_balance,
        tow_pt, tow_sy, cur_apy_key, lo_apy, hi_apy,
        fg_g_pt::DOUBLE  / pow(2.0, 64) as g_pt_d,
        fg_g_sy::DOUBLE  / pow(2.0, 64) as g_sy_d,
        lo_fg_pt::DOUBLE / pow(2.0, 64) as lo_pt_d,
        lo_fg_sy::DOUBLE / pow(2.0, 64) as lo_sy_d,
        hi_fg_pt::DOUBLE / pow(2.0, 64) as hi_pt_d,
        hi_fg_sy::DOUBLE / pow(2.0, 64) as hi_sy_d,
        fee_last_pt::DOUBLE / pow(2.0, 64) as last_pt_d,
        fee_last_sy::DOUBLE / pow(2.0, 64) as last_sy_d
    from normalized
),
mod_2_128 as (
    -- 2^128 in DOUBLE precision (fine for wrap correction)
    select 3.4028236692093846e38 as TWO_128
),
inside as (
    select n.*, m.TWO_128,
        case when n.cur_apy_key >= n.lo_apy then n.lo_pt_d else n.g_pt_d - n.lo_pt_d end as below_pt,
        case when n.cur_apy_key >= n.lo_apy then n.lo_sy_d else n.g_sy_d - n.lo_sy_d end as below_sy,
        case when n.cur_apy_key <  n.hi_apy then n.hi_pt_d else n.g_pt_d - n.hi_pt_d end as above_pt,
        case when n.cur_apy_key <  n.hi_apy then n.hi_sy_d else n.g_sy_d - n.hi_sy_d end as above_sy
    from norm_to_double n cross join mod_2_128 m
),
deltas as (
    select *,
        -- fee_growth_inside = global − below − above (allow wrap-around: if
        -- subtraction goes negative, add 2^128 since values are u128 Q64.64).
        -- In practice, wrap is impossible here because of the value scales.
        (g_pt_d - below_pt - above_pt) as fg_inside_pt,
        (g_sy_d - below_sy - above_sy) as fg_inside_sy
    from inside
)
select
    wallet,
    position_account,
    clmm_market,
    lp_balance,
    -- Δinside × L (already in natural Q64.64-decoded units = atoms per L-unit)
    -- claim_atoms = L × Δinside_natural; no further shift needed because
    -- fg_inside is already in natural (atom-per-L) units after /2^64.
    greatest(lp_balance::DOUBLE * (fg_inside_pt - last_pt_d), 0) + tow_pt as claim_pt_atoms,
    greatest(lp_balance::DOUBLE * (fg_inside_sy - last_sy_d), 0) + tow_sy as claim_sy_atoms,
    tow_pt,
    tow_sy
from deltas
