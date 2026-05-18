-- Per-tx swap signal: user-side underlying capital flow on trade actions.
--
-- For each tx classified as buyYt / sellYt / tradePt, we attribute the
-- "trade notional" as the absolute magnitude of the signer's net underlying-
-- token balance change. Rationale:
--   - Flash-loan-style AMMs (Pendle/Exponent) move many tokens around but
--     the net economic delta to the user IS the trade notional from a
--     capital-deployed perspective.
--   - Matches v1's usdNet approach so dashboard numbers stay comparable.
--   - For "true AMM pool notional" (gross transfers), see a future model
--     int_amm_swaps_gross — not built yet.
--
-- Outputs one row per (signature, market). One swap = one row.
{{ config(materialized='table') }}

with trade_events as (
    select signature, block_time, slot, action
    from {{ ref('int_classified_events') }}
    where action in ('buyYt', 'sellYt', 'tradePt')
),
-- For each trade tx, look at the signer's net underlying-token delta.
-- We need to know which mint is "underlying" for the market the user is
-- trading. Join via SY mint -> dim_markets (TODO: not yet built) — for now
-- we use stg_markets directly.
-- A user-side change is one where owner != known protocol vaults; the
-- simplest filter is: pick the mint that matches a market's underlying.
signer_underlying_deltas as (
    select
        t.signature,
        t.block_time,
        t.action,
        m.market_key,
        m.underlying_mint,
        m.underlying_decimals,
        m.ticker,
        m.platform,
        m.amm_pool,
        m.clmm_orderbook,
        -- Net signer balance change in the underlying token for this market.
        -- Sum because there can be multiple token accounts (ATA + non-ATA).
        coalesce(
            sum(c.delta_ui) filter (where c.delta_raw < 0),  -- outflows from user side
            0
        ) as outflow_ui,
        coalesce(
            sum(c.delta_ui) filter (where c.delta_raw > 0),
            0
        ) as inflow_ui
    from trade_events t
    join {{ ref('stg_helius_tx') }} s using (signature)
    -- Match the tx to a market via accounts touched (any token change
    -- referencing one of the market's known mints).
    join {{ ref('stg_markets') }} m
        on m.source = 'api'
       and m.amm_pool is not null
    join {{ ref('stg_token_changes') }} c
        on c.signature = t.signature
       and c.mint = m.underlying_mint
    group by t.signature, t.block_time, t.action, m.market_key, m.underlying_mint,
             m.underlying_decimals, m.ticker, m.platform, m.amm_pool, m.clmm_orderbook
)
select
    signature,
    block_time,
    to_timestamp(block_time)::date as date,
    market_key,
    ticker,
    platform,
    underlying_mint,
    action,
    amm_pool,
    clmm_orderbook,
    -- Capital flow magnitude: max of |outflow| and |inflow| — the larger leg
    -- is the user's deployed/received capital. For buyYt this is the user's
    -- payment (outflow); for sellYt their proceeds (inflow).
    greatest(abs(outflow_ui), inflow_ui) as notional_underlying,
    case
        when action in ('buyYt', 'sellYt') then 'YT'
        when action = 'tradePt'             then 'PT'
        else 'OTHER'
    end as side,
    case
        when action = 'tradePt' and inflow_ui > abs(outflow_ui) then 'buy'
        when action = 'tradePt'                                  then 'sell'
        when action = 'buyYt'                                    then 'buy'
        when action = 'sellYt'                                   then 'sell'
    end as direction
from signer_underlying_deltas
where greatest(abs(outflow_ui), inflow_ui) > 0
