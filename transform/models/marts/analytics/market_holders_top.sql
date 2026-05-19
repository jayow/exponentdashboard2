-- Top holders per (market_key, leg) — concrete list, not just aggregates.
--
-- For each PT/YT/LP mint in a market, take the top 100 holders by balance
-- on the latest snapshot. Joined to ticker / underlying for display.
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
    from {{ source('raw', 'raw_holders') }}
    group by mint
),
holders_with_mint as (
    select
        ml.market_key, ml.leg, h.mint, h.snapshot_date,
        h.owner, h.amount,
        sum(h.amount) over (partition by h.mint)                       as total_balance,
        row_number() over (partition by h.mint order by h.amount desc) as rk
    from {{ source('raw', 'raw_holders') }} h
    join latest_snap ls on ls.mint = h.mint and ls.snapshot_date = h.snapshot_date
    join mint_legs ml on ml.mint = h.mint
    where h.amount > 0
),
joined as (
    select
        hwm.*,
        m.ticker, m.platform, m.underlying_mint
    from holders_with_mint hwm
    left join {{ ref('dim_markets') }} m using (market_key)
)
select
    j.market_key, j.leg, j.mint, j.snapshot_date,
    j.ticker, j.platform, j.underlying_mint,
    j.owner, j.amount, j.rk,
    j.total_balance,
    j.amount / nullif(j.total_balance, 0) * 100 as share_pct,
    j.amount * p.price_usd                       as usd_value,
    p.price_usd                                  as underlying_price_usd
from joined j
left join {{ ref('stg_prices') }} p
    on p.mint = j.underlying_mint and p.date = j.snapshot_date
where j.rk <= 500
