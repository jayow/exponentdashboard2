-- Daily market share per underlying asset (ticker).
--
-- Two share dimensions:
--   * volume_share_pct — share of cumulative trade volume that day
--   * tvl_share_pct    — share of total TVL that day
--
-- Ticker is the underlying asset (e.g. USX, fragSOL, xSOL). Markets for
-- different maturities of the same ticker get aggregated into a single
-- row per (date, ticker).
{{ config(materialized='table') }}

with vol_by_ticker as (
    select date, ticker, sum(volume_usd) as volume_usd
    from {{ ref('trading_volume_daily') }}
    where volume_usd is not null and ticker is not null
    group by 1, 2
),
tvl_by_ticker as (
    select date, ticker, sum(tvl_usd) as tvl_usd
    from {{ ref('tvl_daily') }}
    where tvl_usd is not null and ticker is not null
    group by 1, 2
),
combined as (
    select
        coalesce(v.date, t.date)     as date,
        coalesce(v.ticker, t.ticker) as ticker,
        v.volume_usd,
        t.tvl_usd
    from vol_by_ticker v
    full outer join tvl_by_ticker t using (date, ticker)
)
select
    date,
    ticker,
    volume_usd,
    sum(volume_usd) over (partition by date)               as total_volume_usd,
    100.0 * volume_usd / nullif(sum(volume_usd) over (partition by date), 0) as volume_share_pct,
    tvl_usd,
    sum(tvl_usd) over (partition by date)                  as total_tvl_usd,
    100.0 * tvl_usd / nullif(sum(tvl_usd) over (partition by date), 0)       as tvl_share_pct
from combined
