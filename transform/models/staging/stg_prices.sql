-- Per-(mint, date) USD price. One row per day. close_usd is the canonical
-- "daily price" used by downstream marts.
--
-- Priority: pyth > jupiter > stable (1.0 fallback for pegged stables when
-- neither real source has data for that date). When multiple sources exist
-- for the same (mint, date), keep only the highest-priority row.
{{ config(materialized='view') }}

with ranked as (
    select
        mint,
        date,
        close_usd,
        open_usd,
        high_usd,
        low_usd,
        volume_usd,
        source,
        row_number() over (
            partition by mint, date
            order by case source
                when 'pyth'    then 0
                when 'jupiter' then 1
                when 'stable'  then 2
                else 3
            end
        ) as rk
    from {{ source('raw', 'raw_prices') }}
)
select
    mint,
    date,
    close_usd as price_usd,
    open_usd,
    high_usd,
    low_usd,
    volume_usd,
    source
from ranked
where rk = 1
  and close_usd is not null
