{{ config(materialized='table') }}

select
    market_key,
    platform,
    ticker,
    underlying_mint,
    underlying_decimals,
    sy_mint,
    maturity_date::date as maturity_date,
    status
from {{ ref('stg_markets') }}
