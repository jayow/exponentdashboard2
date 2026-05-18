-- Per-tx swap signal: user-side underlying capital flow on trade actions.
--
-- For each tx classified as buyYt / sellYt / tradePt, we attribute the
-- "trade notional" as the absolute magnitude of the signer's net underlying-
-- token balance change.
--
-- Matching strategy (revised):
--   1. Identify which MARKET the tx touched by joining stg_token_changes
--      against any of sy/pt/yt/lp mints in stg_markets.
--   2. For that market, sum the signer's delta in the underlying_mint.
--   3. Take max(|outflow|, inflow) as notional_underlying.
--
-- Why not match on underlying_mint directly:
--   Some trade txs only touch SY/PT/YT (the underlying never appears in
--   the token-change diff because flash-loan accounting nets it out at the
--   wallet level). We need to know the market first, then look at the
--   underlying side.
--
-- For "true AMM pool notional" (gross transfers), see followup model
-- int_amm_swaps_gross — not built yet.
{{ config(materialized='table') }}

with trade_events as (
    select signature, block_time, slot, action
    from {{ ref('int_classified_events') }}
    where action in ('buyYt', 'sellYt', 'tradePt')
),
-- Step 1: figure out which market each trade touched. Match on any of the
-- 4 market-identifying mints (sy/pt/yt/lp). A single tx may touch >1 market
-- (rare — migration markets); we pick the first match.
tx_to_market as (
    select distinct
        t.signature,
        t.block_time,
        t.action,
        first_value(m.market_key) over (
            partition by t.signature order by m.maturity_date desc
        ) as market_key
    from trade_events t
    join {{ ref('stg_token_changes') }} c using (signature)
    join {{ ref('stg_markets') }} m
        on m.source = 'api'
       and (c.mint = m.sy_mint
            or c.mint = m.pt_mint
            or c.mint = m.yt_mint
            or c.mint = m.lp_mint)
),
-- Step 2: for the identified market, sum signer-side underlying-mint deltas
signer_underlying_deltas as (
    select
        tm.signature,
        tm.block_time,
        tm.action,
        tm.market_key,
        m.underlying_mint,
        m.underlying_decimals,
        m.ticker,
        m.platform,
        m.amm_pool,
        m.clmm_orderbook,
        coalesce(sum(c.delta_ui) filter (where c.delta_raw < 0), 0) as outflow_ui,
        coalesce(sum(c.delta_ui) filter (where c.delta_raw > 0), 0) as inflow_ui
    from tx_to_market tm
    join {{ ref('stg_markets') }} m
        on m.market_key = tm.market_key and m.source = 'api'
    left join {{ ref('stg_token_changes') }} c
        on c.signature = tm.signature
       and c.mint      = m.underlying_mint
    group by tm.signature, tm.block_time, tm.action, tm.market_key, m.underlying_mint,
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
