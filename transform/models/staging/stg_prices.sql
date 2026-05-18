{{ config(materialized='view') }}

select
    price_key,
    date,
    price_usd,
    source,
    fetched_at
from {{ source('raw', 'raw_prices') }}
where price_usd is not null
