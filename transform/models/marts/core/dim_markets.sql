-- Market dimension. One row per known market_key. API rows preferred.
{{ config(materialized='table') }}

select
    market_key,
    source,
    sy_mint,
    vault,
    pt_mint,
    yt_mint,
    lp_mint,
    amm_pool,
    clmm_orderbook,
    pool,
    underlying_mint,
    ticker,
    underlying_decimals,
    platform,
    maturity_ts,
    maturity_date,
    status,
    interface_type
from {{ ref('stg_markets') }}
