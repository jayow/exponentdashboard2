-- One row per v2 (XPBook) market, joined with v1 dim_markets where ticker
-- matches so we inherit v1 platform/decimals when paired. Also surfaces
-- payload fields (fees, decimals, threshold).
{{ config(materialized='view') }}

with v2 as (
    select
        market_account,
        book_account,
        sy_mint,
        sy_decimals,
        underlying_ticker,
        maturity_ts,
        market_key,
        cast(to_timestamp(maturity_ts) as date) as maturity_date,
        cast(payload->>'$.price_decimals' as integer) as price_decimals,
        cast(payload->>'$.ln_maker_fee'   as double)  as ln_maker_fee,
        cast(payload->>'$.ln_taker_fee'   as double)  as ln_taker_fee,
        cast(payload->>'$.threshold_amount' as bigint) as threshold_amount,
        fetched_at
    from {{ source('raw', 'raw_v2_markets') }}
),
-- Pull v1 ticker → platform map. Note: dim_markets.ticker (not
-- underlying_ticker) — it's the underlying token symbol from the SY mint's
-- Metaplex metadata, same concept as v2's underlying_ticker.
v1_platforms as (
    select
        ticker as underlying_ticker,
        any_value(platform) as v1_platform
    from {{ ref('dim_markets') }}
    where ticker is not null
      and platform is not null
    group by ticker
)
select
    v2.market_account,
    v2.book_account,
    v2.sy_mint,
    v2.sy_decimals,
    v2.underlying_ticker,
    v2.maturity_ts,
    v2.maturity_date,
    v2.market_key,
    coalesce(v1.v1_platform, '') as platform,
    v2.price_decimals,
    v2.ln_maker_fee,
    v2.ln_taker_fee,
    v2.threshold_amount,
    v2.fetched_at
from v2
left join v1_platforms v1
       on v1.underlying_ticker = v2.underlying_ticker
