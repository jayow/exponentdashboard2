-- One row per swap. The canonical "trading volume" source.
--
-- Built from int_amm_swaps. To change the definition of trading volume,
-- edit int_amm_swaps (single source of truth) and rebuild.
--
-- `notional_underlying` = signer's net underlying-token capital flow
--                         (max of |outflow|, inflow). Matches v1's usdNet
--                         shape — user-economic perspective, not
--                         flash-loan-inflated AMM gross.
{{ config(materialized='table') }}

select
    signature,
    block_time,
    to_timestamp(block_time) as block_ts,
    date,
    market_key,
    ticker,
    platform,
    underlying_mint,
    action,
    side,         -- 'PT' / 'YT' (which user-intent class)
    direction,    -- 'buy' / 'sell'
    notional_underlying,
    notional_usd,
    underlying_price_usd,
    price_source,
    amm_pool,
    clmm_orderbook
from {{ ref('int_amm_swaps') }}
