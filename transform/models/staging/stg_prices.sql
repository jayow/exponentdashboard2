-- Per-(mint, date) USD price. Daily granularity.
--
-- Source priority: pyth > jupiter > stable (1.0 fallback for pegged stables).
--
-- Forward-fill: if a mint has price on date D but not D+1, use D's price for
-- D+1 (and onward until the next real datapoint). LSTs barely change minute-
-- to-minute, and stables are nearly constant — forward-filling avoids the
-- "no TVL today because extract_prices hasn't run yet" gotcha.
{{ config(materialized='view') }}

with ranked as (
    select
        mint, date, close_usd, open_usd, high_usd, low_usd, volume_usd, source,
        row_number() over (
            partition by mint, date
            order by case source when 'pyth' then 0 when 'jupiter' then 1 when 'stable' then 2 else 3 end
        ) as rk
    from {{ source('raw', 'raw_prices') }}
),
canonical as (
    select mint, date, close_usd as price_usd, open_usd, high_usd, low_usd, volume_usd, source
    from ranked where rk = 1 and close_usd is not null
),
-- Continuous date axis per mint (first observation -> today)
bounds as (
    select mint, min(date) as first_date from canonical group by mint
),
date_axis as (
    select b.mint, t.date::date as date
    from bounds b,
        unnest(generate_series(b.first_date, current_date, interval 1 day)) as t(date)
),
filled as (
    select
        d.mint, d.date,
        c.price_usd, c.open_usd, c.high_usd, c.low_usd, c.volume_usd, c.source,
        -- carry-forward: index of the latest non-null price at or before this date
        sum(case when c.price_usd is not null then 1 else 0 end)
            over (partition by d.mint order by d.date) as price_grp
    from date_axis d
    left join canonical c on c.mint = d.mint and c.date = d.date
),
ffilled as (
    select
        f.mint,
        f.date,
        -- carry-forward: pick last non-null price at or before this row
        coalesce(
            f.price_usd,
            last_value(f.price_usd ignore nulls) over (
                partition by f.mint order by f.date rows between unbounded preceding and current row
            )
        ) as price_usd,
        f.open_usd,
        f.high_usd,
        f.low_usd,
        f.volume_usd,
        coalesce(
            f.source,
            last_value(f.source ignore nulls) over (
                partition by f.mint order by f.date rows between unbounded preceding and current row
            ) || '_ffill'
        ) as source
    from filled f
)
select * from ffilled where price_usd is not null
