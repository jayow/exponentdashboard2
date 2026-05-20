-- Per (market_key, leg) holder concentration snapshot.
--
-- Sources from int_holders_current — which unions:
--   PT  from raw_holders (getProgramAccounts SPL token-account scan), and
--   YT/LP from raw_positions (YieldTokenPosition + LpPosition Anchor
--   accounts under the Exponent core program).
--
-- We aggregate to: total holders, top-1/top-5/top-10 concentration %,
-- supply distribution metrics. snapshot_date is set to current_date by
-- int_holders_current — i.e., "as of the latest extract".
{{ config(materialized='table') }}

with mint_legs as (
    select market_key, pt_mint as mint, 'PT' as leg from {{ ref('dim_markets') }} where pt_mint is not null
    union all
    select market_key, yt_mint, 'YT' from {{ ref('dim_markets') }} where yt_mint is not null
    union all
    select market_key, lp_mint, 'LP' from {{ ref('dim_markets') }} where lp_mint is not null
),
latest_snap as (
    select mint, max(snapshot_date) as snapshot_date
    from {{ ref('int_holders_current') }}
    group by mint
),
ranked as (
    select
        ml.market_key,
        ml.leg,
        h.mint,
        h.snapshot_date,
        h.owner,
        h.amount,
        sum(h.amount) over (partition by h.mint)                          as total_supply,
        row_number() over (partition by h.mint order by h.amount desc)    as rk
    from {{ ref('int_holders_current') }} h
    join latest_snap ls on ls.mint = h.mint and ls.snapshot_date = h.snapshot_date
    join mint_legs ml on ml.mint = h.mint
    where h.amount > 0
),
by_mint as (
    select
        market_key, leg, mint, snapshot_date,
        max(total_supply)                                                          as total_supply,
        count(distinct owner)                                                      as n_holders,
        sum(case when rk = 1  then amount else 0 end) / nullif(max(total_supply), 0) * 100 as top1_pct,
        sum(case when rk <= 5 then amount else 0 end) / nullif(max(total_supply), 0) * 100 as top5_pct,
        sum(case when rk <= 10 then amount else 0 end) / nullif(max(total_supply), 0) * 100 as top10_pct,
        max(amount)                                                                as max_amount
    from ranked
    group by market_key, leg, mint, snapshot_date
),
ticker_join as (
    select b.*, m.ticker, m.platform, m.maturity_date, m.status
    from by_mint b
    left join {{ ref('dim_markets') }} m using (market_key)
)
select * from ticker_join
