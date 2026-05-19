-- SY → underlying exchange rate, derived from on-chain mint/burn events.
--
-- For every transaction that net-mints SY (supply increases), find the matching
-- underlying inflow into the vault PDA. Rate = underlying_in / sy_minted.
--
-- Conservation rule: when SY is minted via deposit, X underlying flows in and
-- Y SY is minted out. The instantaneous SY:underlying rate is X/Y.
--
-- Restricted to "pure deposit" txs (|underlying_in − sy_minted| / sy_minted < 0.5)
-- to filter out multi-instruction txs where the underlying delta is contaminated
-- by routes through other accounts (LP, swaps, etc.).
--
-- Empirical result on Exponent: rate ≡ 1.0 across all SY mints (USX, eUSX,
-- ONyc, BulkSOL, fragSOL, hyloSOL, xSOL, hyUSD all observed with min=1.0,
-- avg=1.00001). Exponent SY tokens are pure 1:1 wrappers; yield accrual lives
-- in the underlying LST itself which Pyth/Jupiter already price correctly.
-- Kept as a derived model (not a constant) so the pipeline catches any future
-- rebasing SY automatically.
--
-- Daily aggregation: median rate per (sy_mint, date) over observed events,
-- forward-filled for dates with no events.
{{ config(materialized='view') }}

with sy_mints as (
    select distinct sy_mint, underlying_mint
    from {{ ref('stg_markets') }}
    where sy_mint is not null and underlying_mint is not null
),
sy_net_per_tx as (
    -- Transactions where the SY mint's total supply increased (mint events)
    select c.signature, c.mint as sy_mint,
           to_timestamp(c.block_time)::date as date,
           sum(c.delta_ui) as net_sy_minted
    from {{ ref('stg_token_changes') }} c
    join sy_mints s on s.sy_mint = c.mint
    group by 1, 2, 3
    having sum(c.delta_ui) > 0.01
),
und_in_per_tx as (
    select c.signature, c.mint as und_mint,
           sum(case when c.delta_ui > 0 then c.delta_ui else 0 end) as und_in
    from {{ ref('stg_token_changes') }} c
    join sy_mints s on s.underlying_mint = c.mint
    group by 1, 2
),
paired as (
    select sy.signature, sy.sy_mint, sy.date, sy.net_sy_minted,
           u.und_in,
           u.und_in / sy.net_sy_minted as rate
    from sy_net_per_tx sy
    join sy_mints m on m.sy_mint = sy.sy_mint
    join und_in_per_tx u on u.signature = sy.signature and u.und_mint = m.underlying_mint
    where u.und_in > 0
      -- Pure-deposit filter: drop multi-ix txs where underlying side is contaminated
      and abs(u.und_in - sy.net_sy_minted) / sy.net_sy_minted < 0.5
),
daily as (
    select sy_mint, date,
           median(rate) as rate,
           count(*)     as n_events
    from paired
    group by 1, 2
),
-- Continuous date axis per sy_mint with forward-fill (rates change slowly,
-- and we want a value for every day even if no events that day)
bounds as (
    select sy_mint, min(date) as first_date from daily group by 1
),
date_axis as (
    select b.sy_mint, t.date::date as date
    from bounds b,
        unnest(generate_series(b.first_date, current_date, interval 1 day)) as t(date)
),
filled as (
    select d.sy_mint, d.date,
           dl.rate,
           dl.n_events,
           sum(case when dl.rate is not null then 1 else 0 end)
               over (partition by d.sy_mint order by d.date) as grp
    from date_axis d
    left join daily dl on dl.sy_mint = d.sy_mint and dl.date = d.date
)
select
    f.sy_mint,
    f.date,
    coalesce(
        f.rate,
        last_value(f.rate ignore nulls) over (
            partition by f.sy_mint order by f.date
            rows between unbounded preceding and current row
        )
    ) as rate,
    f.n_events
from filled f
qualify rate is not null
