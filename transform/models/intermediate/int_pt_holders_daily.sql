-- Daily protocol-wide PT holder count (distinct wallets), restricted to
-- active (unmatured-as-of-each-date) markets, reconstructed from
-- stg_token_changes.
--
-- A wallet is a "PT holder on date D" iff there exists at least one PT
-- mint M where:
--   - the wallet's running balance in mint M is > 0 at end of date D
--   - and dim_markets.maturity_date(M) >= D
--
-- Output: one row per date, with the count of distinct wallets satisfying
-- the above across ALL active PT mints (a wallet holding PT in 5 markets
-- counts ONCE). Today and any other snapshot dates are overridden with
-- the on-chain raw_holders count for exactness.
--
-- Known PT-specific limitation: our signature crawl misses plain
-- wallet-to-wallet PT transfers between two uncrawled addresses. The
-- reconstructed count drifts upward over time vs the snapshot truth.
-- The today-anchoring override fixes the most recent point exactly.
{{ config(materialized='table') }}

with pt_mints as (
    select pt_mint as mint, market_key, maturity_date
    from {{ ref('dim_markets') }}
    where pt_mint is not null
),
-- Per-(mint, owner, date) net delta_raw.
daily_delta as (
    select
        c.mint,
        c.owner,
        to_timestamp(c.block_time)::date as date,
        sum(c.delta_raw)::hugeint as daily_delta
    from {{ ref('stg_token_changes') }} c
    join pt_mints m on m.mint = c.mint
    group by 1, 2, 3
),
-- Running balance per (mint, owner), with the date the next delta happens
-- (or current_date+1 as a sentinel) so we can build [start, end) intervals.
running as (
    select
        mint, owner, date,
        sum(daily_delta) over (partition by mint, owner order by date) as bal_raw,
        lead(date, 1, current_date + interval 1 day) over (partition by mint, owner order by date) as next_date
    from daily_delta
),
-- Active holding intervals per (mint, owner) — capped at maturity_date so
-- an owner stops being counted once their mint's market expires.
intervals as (
    select
        r.mint,
        r.owner,
        r.date                                     as start_date,
        least((r.next_date - interval 1 day)::date, mt.maturity_date) as end_date
    from running r
    join pt_mints mt on mt.mint = r.mint
    where r.bal_raw > 0
      and r.date <= mt.maturity_date
),
-- Expand intervals into per-day (owner, date) flags. A given owner is
-- counted on each date in any of their PT intervals.
date_owner as (
    select distinct i.owner, t.date::date as date
    from intervals i,
        unnest(generate_series(i.start_date, i.end_date, interval 1 day)) as t(date)
),
counts_recon as (
    select date, count(distinct owner) as n_holders
    from date_owner
    group by date
),
-- Snapshot truth (active-market filtered).
counts_snap as (
    select
        h.snapshot_date as date,
        count(distinct h.owner) as n_holders
    from {{ source('raw', 'raw_holders') }} h
    join pt_mints m on m.mint = h.mint
    where h.amount > 0 and m.maturity_date >= h.snapshot_date
    group by 1
),
-- Defensive: cover any snapshot dates that don't appear in recon (and vice versa).
all_dates as (
    select date from counts_recon
    union
    select date from counts_snap
)
select
    d.date,
    'PT' as leg,
    coalesce(s.n_holders, r.n_holders, 0)::bigint as n_holders,
    case when s.n_holders is not null then 'snapshot' else 'reconstructed' end as source
from all_dates d
left join counts_recon r on r.date = d.date
left join counts_snap  s on s.date = d.date
