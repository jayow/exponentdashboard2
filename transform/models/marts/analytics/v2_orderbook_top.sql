-- Per-market-per-book ladder, top ~200 per side. Ordered so the JSON
-- emitter can splice without re-sorting:
--   bid rows: rk ascending = APY descending (best bid first)
--   ask rows: rk ascending = APY ascending (best ask first)
{{ config(materialized='table') }}

with ranked as (
    select
        market_key,
        book_account,
        side,
        owner_wallet,
        apy_rate,
        size_sy,
        size_sy_atomic,
        expiry_at,
        created_at,
        virtual_offer,
        row_number() over (
            partition by market_key, book_account, side
            order by case when side = 'bid' then -apy_rate else apy_rate end,
                     created_at
        ) as rk
    from {{ ref('int_v2_orderbook_latest') }}
    where side in ('bid','ask')
)
select *
from ranked
where rk <= 200
