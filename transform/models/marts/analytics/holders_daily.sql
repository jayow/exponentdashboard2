-- Daily holder count per leg (PT, YT, LP), in ACTIVE markets only.
-- A wallet contributes to date D iff they hold the leg's token in at
-- least one market that was unmatured as of D. Counted once per leg —
-- a wallet holding YT in multiple active markets is one YT holder.
--
-- See int_pt_holders_daily and int_yt_lp_holders_daily for derivation
-- (PT: running balance from stg_token_changes; YT/LP: active-range
-- reconstruction from Anchor event logs in stg_anchor_position_events).
-- Today and other snapshot dates are anchored to on-chain truth.
{{ config(materialized='table') }}

select date, leg, n_holders, source from {{ ref('int_pt_holders_daily') }}
union all
select date, leg, n_holders, source from {{ ref('int_yt_lp_holders_daily') }}
order by date, leg
