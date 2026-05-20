-- Daily YT and LP holder count in ACTIVE (unmatured) markets, reconstructed
-- from Anchor instruction events tagged with their market_key.
--
-- On date D we count a wallet as a "holder" iff:
--   1. they have a position-mutating event for (market_key, leg) on or before D
--   2. they either hold today in that (market_key, leg), OR their last
--      event for that (market_key, leg) was on or after D — "active range"
--   3. the market's maturity_date >= D — i.e. the market was still live
--      at that historical date
--
-- A wallet holding YT in multiple active markets is counted ONCE per leg
-- at the protocol level (distinct signer across markets).
--
-- Today's value is overridden with the raw_positions snapshot (joined to
-- dim_markets and filtered to active markets) so the anchor matches the
-- per-market view on the markets table.
{{ config(materialized='table') }}

with events as (
    select
        to_timestamp(block_time)::date as date,
        signer,
        leg,
        market_key
    from {{ ref('stg_anchor_position_events') }}
    where leg in ('YT', 'LP') and signer is not null and market_key is not null
),
maturity as (
    select market_key, maturity_date from {{ ref('dim_markets') }}
),
latest_snap_pos as (
    select max(snapshot_date) as d from {{ source('raw', 'raw_positions') }}
),
-- Currently-holding wallets per (market_key, leg), restricted to markets
-- that are still active today. This is the anchor for "still_holding".
holders_today as (
    select distinct
        p.owner as signer,
        case when p.leg = 'YT' then 'YT' else 'LP' end as leg,
        m.market_key
    from {{ source('raw', 'raw_positions') }} p
    join {{ ref('dim_markets') }} m on (
            (p.leg = 'YT'      and m.vault    = p.vault)
         or (p.leg = 'LP'      and m.amm_pool = p.vault)
         or (p.leg = 'LP_CLMM' and p.vault in (
             select clmm_market from {{ source('raw', 'raw_clmm_markets') }}
             where pt_mint = m.pt_mint
         ))
    ),
    latest_snap_pos ls
    where p.snapshot_date = ls.d and p.amount_raw > 0
      and m.maturity_date >= ls.d
),
-- Defensive: include holders today even if we have no parsed events for them.
all_signers as (
    select signer, leg, market_key, date from events
    union all
    select signer, leg, market_key, (select d from latest_snap_pos) as date
    from holders_today
),
ranges as (
    select
        a.signer,
        a.leg,
        a.market_key,
        min(a.date) as first_date,
        max(a.date) as last_date,
        (h.signer is not null) as still_holding
    from all_signers a
    left join holders_today h
      on h.signer = a.signer and h.leg = a.leg and h.market_key = a.market_key
    group by a.signer, a.leg, a.market_key, h.signer
),
ranges_m as (
    select r.*, mt.maturity_date
    from ranges r join maturity mt on mt.market_key = r.market_key
),
date_axis as (
    select t.date::date as date
    from unnest(generate_series(
        (select min(first_date) from ranges_m),
        current_date,
        interval 1 day
    )) as t(date)
),
-- Per date, count distinct signers per leg active in any unmatured market.
active as (
    select d.date, r.leg, count(distinct r.signer) as n_holders_recon
    from date_axis d
    join ranges_m r
      on d.date >= r.first_date
     and (r.still_holding or d.date <= r.last_date)
     and r.maturity_date >= d.date
    group by 1, 2
),
-- Snapshot truth (active-market filtered) from raw_positions.
snap as (
    select
        p.snapshot_date as date,
        case when p.leg = 'YT' then 'YT' else 'LP' end as leg,
        count(distinct p.owner) as n_holders_snap
    from {{ source('raw', 'raw_positions') }} p
    join {{ ref('dim_markets') }} m on (
            (p.leg = 'YT'      and m.vault    = p.vault)
         or (p.leg = 'LP'      and m.amm_pool = p.vault)
         or (p.leg = 'LP_CLMM' and p.vault in (
             select clmm_market from {{ source('raw', 'raw_clmm_markets') }}
             where pt_mint = m.pt_mint
         ))
    )
    where p.amount_raw > 0
      and m.maturity_date >= p.snapshot_date
    group by 1, 2
)
select
    a.date,
    a.leg,
    coalesce(s.n_holders_snap, a.n_holders_recon)::bigint as n_holders,
    case when s.n_holders_snap is not null then 'snapshot' else 'reconstructed' end as source
from active a
left join snap s on s.date = a.date and s.leg = a.leg
union all
select s.date, s.leg, s.n_holders_snap::bigint, 'snapshot'
from snap s
left join active a on a.date = s.date and a.leg = s.leg
where a.date is null
