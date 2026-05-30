-- One row per market_key. Includes markets with 0 resting orders (so the
-- frontend can still render the "v2 book exists" badge). When a market has
-- multiple v2 books (e.g. BulkSOL-20JUN26 ×2) the row aggregates across all
-- books and picks the one with the most offers as the "primary".
{{ config(materialized='table') }}

with all_markets as (
    select
        market_key,
        underlying_ticker,
        platform,
        sy_mint,
        book_account
    from {{ ref('stg_v2_markets') }}
),
per_book as (
    select
        m.market_key,
        m.book_account,
        m.underlying_ticker,
        m.platform,
        m.sy_mint,
        any_value(o.snapshot_date)                       as snapshot_date,
        count(o.side) filter (where o.side in ('bid','ask')) as n_offers,
        count(o.side) filter (where o.side = 'bid')      as n_bids,
        count(o.side) filter (where o.side = 'ask')      as n_asks,
        max(o.apy_rate) filter (where o.side = 'bid')    as best_bid_apy,
        min(o.apy_rate) filter (where o.side = 'ask')    as best_ask_apy,
        sum(o.size_sy) filter (where o.side = 'bid')     as total_bid_size_sy,
        sum(o.size_sy) filter (where o.side = 'ask')     as total_ask_size_sy,
        sum(o.size_sy_atomic) filter (where o.side = 'bid') as total_bid_size_atomic,
        sum(o.size_sy_atomic) filter (where o.side = 'ask') as total_ask_size_atomic
    from all_markets m
    left join {{ ref('int_v2_orderbook_latest') }} o
           on o.book_account = m.book_account
    group by m.market_key, m.book_account, m.underlying_ticker, m.platform, m.sy_mint
),
ranked as (
    select
        *,
        row_number() over (
            partition by market_key
            order by n_offers desc nulls last, book_account
        ) as book_rank,
        count(*) over (partition by market_key) as book_count
    from per_book
),
per_market as (
    select
        market_key,
        any_value(underlying_ticker)        as ticker,
        any_value(platform)                 as platform,
        any_value(sy_mint)                  as sy_mint,
        any_value(snapshot_date)            as snapshot_date,
        any_value(book_account) filter (where book_rank = 1) as primary_book_account,
        any_value(book_count)               as book_count,
        sum(n_offers)                       as n_offers,
        sum(n_bids)                         as n_bids,
        sum(n_asks)                         as n_asks,
        max(best_bid_apy)                   as best_bid_apy,
        min(best_ask_apy)                   as best_ask_apy,
        sum(total_bid_size_sy)              as total_bid_size_sy,
        sum(total_ask_size_sy)              as total_ask_size_sy,
        sum(total_bid_size_atomic)          as total_bid_size_atomic,
        sum(total_ask_size_atomic)          as total_ask_size_atomic
    from ranked
    group by market_key
)
select
    market_key,
    ticker,
    platform,
    sy_mint,
    snapshot_date,
    primary_book_account,
    book_count,
    n_offers, n_bids, n_asks,
    best_bid_apy,
    best_ask_apy,
    case
        when best_bid_apy is null or best_ask_apy is null then null
        else (best_bid_apy + best_ask_apy) / 2.0
    end as mid_apy,
    case
        when best_bid_apy is null or best_ask_apy is null then null
        else (best_ask_apy - best_bid_apy) * 10000
    end as spread_bps,
    total_bid_size_sy,
    total_ask_size_sy,
    total_bid_size_atomic,
    total_ask_size_atomic
from per_market
