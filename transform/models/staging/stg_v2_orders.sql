-- v2 resting orders, typed and joined to market metadata.
-- One row per (snapshot_date, book_account, offer_idx).
{{ config(materialized='view') }}

select
    o.snapshot_date,
    o.book_account,
    m.market_key,
    m.underlying_ticker,
    m.platform,
    m.sy_mint,
    coalesce(m.sy_decimals, 9) as sy_decimals,
    o.offer_idx,
    o.owner_wallet,
    o.side,                              -- 'bid' | 'ask' | '?'
    o.apy_rate,                          -- decimal e.g. 0.1225 = 12.25%
    o.size_sy_atomic,
    -- SY tokens: decimals vary per market (6 for USX/stSLX/etc., 9 for SOL LSTs)
    o.size_sy_atomic::double / pow(10, coalesce(m.sy_decimals, 9)) as size_sy,
    o.expiry_at,
    o.created_at,
    cast(to_timestamp(o.expiry_at)  as timestamp) as expiry_ts,
    cast(to_timestamp(o.created_at) as timestamp) as created_ts,
    o.type_flag,
    o.virtual_offer,
    o.fok
from {{ source('raw', 'raw_v2_orders_snapshots') }} o
left join {{ ref('stg_v2_markets') }} m
       on m.book_account = o.book_account
