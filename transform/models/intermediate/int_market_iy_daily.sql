-- Daily implied-yield series per market — the source for the market page's
-- Implied Yield chart.
--
-- Two sources, best-first per (market, date):
--   1. 'curve'  — daily snapshot of the SDK time-curve rate from
--      raw_market_two_pools: IY = exp(last_ln_implied_rate) − 1. Canonical
--      (matches Exponent's displayed Implied APY); accrues one point per
--      daily pipeline run.
--   2. 'trades' — notional-weighted mean of YT-derived execution IY from
--      int_swap_execution_iy. Backfills dates before snapshots existed.
--      YT-derived ONLY: PT-derived execution prices are systematically low
--      on markets whose SY wrapper accrues an exchange rate (weUSX ×1.035,
--      wONyc ×1.116 — PT redeems one SY unit, not one raw underlying), which
--      annualization amplified into 3–5× IY errors. YT prices are small, so
--      the same unit skew is negligible there (validated within ~1.5pp of
--      curve snapshots on overlapping dates).
--
-- Dates on/after maturity are excluded — annualization degenerates as
-- days-to-maturity → 0 (maturity-day snapshots read 11–17% on markets
-- trading at 6%).
{{ config(materialized='table') }}

with curve as (
    select market_key, date, iy
    from (
        select
            m.market_key,
            p.snapshot_date                      as date,
            exp(p.last_ln_implied_rate) - 1.0    as iy,
            row_number() over (
                partition by m.market_key, p.snapshot_date
                order by p.pt_balance_raw desc
            ) as rn
        from {{ source('raw', 'raw_market_two_pools') }} p
        join {{ ref('stg_markets') }} m on m.pt_mint = p.mint_pt
        where p.last_ln_implied_rate is not null
    )
    where rn = 1
),
trades as (
    select
        market_key,
        to_timestamp(block_time)::date as date,
        sum(entry_iy * notional_underlying) / nullif(sum(notional_underlying), 0) as iy,
        count(*) as n_trades
    from {{ ref('int_swap_execution_iy') }}
    where leg = 'YT'
    group by 1, 2
),
combined as (
    select market_key, date, iy, 'curve' as source, null::int as n_trades
    from curve
    union all
    select t.market_key, t.date, t.iy, 'trades', t.n_trades
    from trades t
    left join curve c using (market_key, date)
    where c.market_key is null
)
select c.market_key, c.date, c.iy, c.source, c.n_trades
from combined c
join {{ ref('stg_markets') }} m using (market_key)
where m.maturity_date is not null
  and c.date < m.maturity_date
