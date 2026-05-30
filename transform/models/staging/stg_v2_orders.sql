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
    o.offer_idx,
    o.owner_wallet,
    o.side,                              -- 'bid' | 'ask' | '?'
    o.apy_rate,                          -- decimal e.g. 0.1225 = 12.25%
    o.size_sy_atomic,
    -- SY tokens (Exponent wrapped tokens are consistently 9 decimals)
    o.size_sy_atomic::double / 1e9 as size_sy,
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
