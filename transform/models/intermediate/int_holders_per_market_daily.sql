-- Per-market daily holder count per leg (PT, YT, LP), restricted to
-- dates when the market was still active (maturity_date >= date).
--
-- One row per (date, market_key, leg). A wallet holding the leg's token
-- in multiple markets contributes one row per market. The protocol-wide
-- distinct-wallet view is in `holders_daily`.
--
-- Methodology mirrors the protocol-level models:
--   - PT  : running balance per (pt_mint, owner, date) from stg_token_changes
--   - YT/LP: active-range per (signer, market_key, leg) from
--            stg_anchor_position_events (events tagged with market_key
--            via tx accountKeys ↔ dim_markets pubkeys)
--
-- Today is anchored to raw_holders (PT) / raw_positions (YT, LP) snapshot
-- truth, joined to dim_markets and filtered to active markets.
{{ config(materialized='table') }}

-- ═══ PT branch ═══════════════════════════════════════════════════════════
with pt_mints as (
    select pt_mint as mint, market_key, maturity_date
    from {{ ref('dim_markets') }}
    where pt_mint is not null
),
pt_delta as (
    select c.mint, c.owner,
           to_timestamp(c.block_time)::date as date,
           sum(c.delta_raw)::hugeint as daily_delta
    from {{ ref('stg_token_changes') }} c
    join pt_mints m on m.mint = c.mint
    group by 1, 2, 3
),
pt_running as (
    select mint, owner, date,
           sum(daily_delta) over (partition by mint, owner order by date) as bal_raw,
           lead(date, 1, current_date + interval 1 day)
             over (partition by mint, owner order by date) as next_date
    from pt_delta
),
pt_intervals as (
    select r.mint, r.owner, r.date as start_date,
           least((r.next_date - interval 1 day)::date, m.maturity_date) as end_date,
           m.market_key
    from pt_running r
    join pt_mints m on m.mint = r.mint
    where r.bal_raw > 0 and r.date <= m.maturity_date
),
pt_recon as (
    select i.market_key, t.date::date as date,
           count(distinct i.owner) as n_holders
    from pt_intervals i,
        unnest(generate_series(i.start_date, i.end_date, interval 1 day)) as t(date)
    group by 1, 2
),
pt_snap as (
    select m.market_key, h.snapshot_date as date,
           count(distinct h.owner) as n_holders
    from {{ source('raw', 'raw_holders') }} h
    join pt_mints m on m.mint = h.mint
    where h.amount > 0 and m.maturity_date >= h.snapshot_date
    group by 1, 2
),

-- ═══ YT/LP branch ═══════════════════════════════════════════════════════
events as (
    select to_timestamp(block_time)::date as date,
           signer, leg, market_key
    from {{ ref('stg_anchor_position_events') }}
    where leg in ('YT', 'LP') and signer is not null and market_key is not null
),
latest_snap_pos as (
    select max(snapshot_date) as d from {{ source('raw', 'raw_positions') }}
),
holders_today as (
    select distinct p.owner as signer,
           case when p.leg = 'YT' then 'YT' else 'LP' end as leg,
           m.market_key
    from {{ source('raw', 'raw_positions') }} p
    join {{ ref('dim_markets') }} m on (
            (p.leg = 'YT' and m.vault    = p.vault)
         or (p.leg = 'LP' and m.amm_pool = p.vault)
         or (p.leg = 'LP_CLMM' and p.vault in (
                select clmm_market from {{ source('raw', 'raw_clmm_markets') }}
                where pt_mint = m.pt_mint
            ))
    ), latest_snap_pos ls
    where p.snapshot_date = ls.d and p.amount_raw > 0
      and m.maturity_date >= ls.d
),
all_signers as (
    select signer, leg, market_key, date from events
    union all
    select signer, leg, market_key, (select d from latest_snap_pos) from holders_today
),
ranges as (
    select a.signer, a.leg, a.market_key,
           min(a.date) as first_date, max(a.date) as last_date,
           (h.signer is not null) as still_holding
    from all_signers a
    left join holders_today h
      on h.signer = a.signer and h.leg = a.leg and h.market_key = a.market_key
    group by a.signer, a.leg, a.market_key, h.signer
),
ranges_m as (
    select r.*, m.maturity_date
    from ranges r join {{ ref('dim_markets') }} m on m.market_key = r.market_key
),
date_axis as (
    select t.date::date as date
    from unnest(generate_series(
        (select min(first_date) from ranges_m),
        current_date,
        interval 1 day
    )) as t(date)
),
ytlp_recon as (
    select d.date, r.market_key, r.leg, count(distinct r.signer) as n_holders
    from date_axis d
    join ranges_m r on d.date >= r.first_date
                    and (r.still_holding or d.date <= r.last_date)
                    and r.maturity_date >= d.date
    group by 1, 2, 3
),
ytlp_snap as (
    select p.snapshot_date as date, m.market_key,
           case when p.leg = 'YT' then 'YT' else 'LP' end as leg,
           count(distinct p.owner) as n_holders
    from {{ source('raw', 'raw_positions') }} p
    join {{ ref('dim_markets') }} m on (
            (p.leg = 'YT' and m.vault    = p.vault)
         or (p.leg = 'LP' and m.amm_pool = p.vault)
         or (p.leg = 'LP_CLMM' and p.vault in (
                select clmm_market from {{ source('raw', 'raw_clmm_markets') }}
                where pt_mint = m.pt_mint
            ))
    )
    where p.amount_raw > 0 and m.maturity_date >= p.snapshot_date
    group by 1, 2, 3
),

-- ═══ Combine: PT then YT then LP ═════════════════════════════════════════
combined_pt as (
    select coalesce(s.market_key, r.market_key) as market_key,
           coalesce(s.date, r.date) as date,
           'PT' as leg,
           coalesce(s.n_holders, r.n_holders, 0)::bigint as n_holders,
           case when s.n_holders is not null then 'snapshot' else 'reconstructed' end as source
    from pt_recon r
    full outer join pt_snap s on s.market_key = r.market_key and s.date = r.date
),
combined_ytlp as (
    select coalesce(s.market_key, r.market_key) as market_key,
           coalesce(s.date, r.date) as date,
           coalesce(s.leg, r.leg) as leg,
           coalesce(s.n_holders, r.n_holders, 0)::bigint as n_holders,
           case when s.n_holders is not null then 'snapshot' else 'reconstructed' end as source
    from ytlp_recon r
    full outer join ytlp_snap s
      on s.market_key = r.market_key and s.date = r.date and s.leg = r.leg
)
select * from combined_pt
union all
select * from combined_ytlp
