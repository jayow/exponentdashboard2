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
-- For each (signature, market) pair, compute the strongest match-signal:
-- PT/YT/LP mints are unique per market, SY mints are shared across maturities.
-- A match via PT/YT/LP wins over a match via SY for the same tx.
tx_market_matches as (
    select distinct
        t.signature,
        t.block_time,
        t.action,
        m.market_key,
        m.source,
        m.maturity_ts,
        -- match_strength: 2 = via PT/YT/LP (unique), 1 = via SY (shared)
        max(case
            when c.mint = m.pt_mint then 2
            when c.mint = m.yt_mint then 2
            when c.mint = m.lp_mint then 2
            when c.mint = m.sy_mint then 1
            else 0
        end) as match_strength
    from trade_events t
    join {{ ref('stg_token_changes') }} c using (signature)
    join {{ ref('stg_markets') }} m
        on m.underlying_mint is not null
       and (c.mint = m.sy_mint
            or c.mint = m.pt_mint
            or c.mint = m.yt_mint
            or c.mint = m.lp_mint)
    group by t.signature, t.block_time, t.action, m.market_key, m.source, m.maturity_ts
),
-- Pick the single best market per signature.
tx_to_market as (
    select distinct
        signature, block_time, action,
        first_value(market_key) over (
            partition by signature
            order by
                match_strength desc,                                    -- unique PT/YT match beats shared SY match
                case when source = 'api' then 0 else 1 end,             -- API metadata richest
                abs(coalesce(maturity_ts, 0) - block_time)              -- closest maturity wins
        ) as market_key
    from tx_market_matches
),
-- Step 2: for the identified market, compute two aggregates:
--   * all_*  — sum across all parties touching the underlying mint (notional)
--   * signer_* — restrict to deltas where owner = signer (definitive direction)
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
        -- All-party flows (robust notional — captures even routed/wrapper flows)
        coalesce(sum(c.delta_ui) filter (where c.delta_raw < 0), 0)                          as all_outflow_ui,
        coalesce(sum(c.delta_ui) filter (where c.delta_raw > 0), 0)                          as all_inflow_ui,
        -- Signer-only flows (clear direction when present)
        coalesce(sum(c.delta_ui) filter (where c.delta_raw < 0 and c.owner = h.signer), 0)   as signer_outflow_ui,
        coalesce(sum(c.delta_ui) filter (where c.delta_raw > 0 and c.owner = h.signer), 0)   as signer_inflow_ui
    from tx_to_market tm
    join {{ ref('stg_helius_tx') }} h on h.signature = tm.signature
    join {{ ref('stg_markets') }} m
        on m.market_key = tm.market_key
       and m.underlying_mint is not null
    left join {{ ref('stg_token_changes') }} c
        on c.signature = tm.signature
       and c.mint      = m.underlying_mint
    group by tm.signature, tm.block_time, tm.action, tm.market_key, m.underlying_mint,
             m.underlying_decimals, m.ticker, m.platform, m.amm_pool, m.clmm_orderbook,
             h.signer
),
-- Matched trades (have a market_key + underlying delta).
matched_trades as (
    select
        signature,
        block_time,
        action,
        market_key,
        ticker,
        platform,
        underlying_mint,
        amm_pool,
        clmm_orderbook,
        all_outflow_ui     as outflow_ui,
        all_inflow_ui      as inflow_ui,
        signer_outflow_ui,
        signer_inflow_ui
    from signer_underlying_deltas
    where greatest(abs(all_outflow_ui), all_inflow_ui) > 0
),
-- Unclassified trades: action is buyYt/sellYt/tradePt but we couldn't
-- resolve a market. Fall back to using the user's largest absolute delta
-- in ANY token Exponent considers an underlying (raw_exponent_tokens).
-- Mark with market_key='UNCLASSIFIED' so the dashboard can show them as
-- a separate aggregate without forcing a market attribution.
unclassified_deltas as (
    select
        t.signature,
        t.block_time,
        t.action,
        et.mint                                      as underlying_mint,
        et.symbol                                    as ticker,
        sum(c.delta_ui) filter (where c.delta_raw < 0)  as outflow_ui,
        sum(c.delta_ui) filter (where c.delta_raw > 0)  as inflow_ui,
        sum(abs(c.delta_ui))                          as total_abs
    from trade_events t
    join {{ ref('stg_token_changes') }} c using (signature)
    join {{ source('raw', 'raw_exponent_tokens') }} et on et.mint = c.mint
    -- Fire for ANY sig that didn't produce a matched_trades row.
    -- Covers both "never matched a market" and "matched but zero underlying delta".
    where t.signature not in (select signature from matched_trades)
    group by t.signature, t.block_time, t.action, et.mint, et.symbol
),
-- For each unclassified sig, pick the SINGLE underlying mint with the
-- largest |delta| total — that's the user's "real" trade asset.
unclassified_best as (
    select distinct
        signature, block_time, action,
        first_value(underlying_mint) over (
            partition by signature order by total_abs desc
        ) as underlying_mint,
        first_value(ticker) over (
            partition by signature order by total_abs desc
        ) as ticker,
        first_value(coalesce(outflow_ui, 0)) over (
            partition by signature order by total_abs desc
        ) as outflow_ui,
        first_value(coalesce(inflow_ui, 0)) over (
            partition by signature order by total_abs desc
        ) as inflow_ui
    from unclassified_deltas
),
unioned as (
    select
        signature, block_time, action, market_key, ticker, platform,
        underlying_mint, amm_pool, clmm_orderbook,
        outflow_ui, inflow_ui,
        signer_outflow_ui, signer_inflow_ui
    from matched_trades
    union all
    select
        signature, block_time, action,
        'UNCLASSIFIED'                  as market_key,
        ticker,
        cast(null as varchar)           as platform,
        underlying_mint,
        cast(null as varchar)           as amm_pool,
        cast(null as varchar)           as clmm_orderbook,
        coalesce(outflow_ui, 0)         as outflow_ui,
        coalesce(inflow_ui, 0)          as inflow_ui,
        -- Unclassified path has no signer split; use the same numbers as fallback
        coalesce(outflow_ui, 0)         as signer_outflow_ui,
        coalesce(inflow_ui, 0)          as signer_inflow_ui
    from unclassified_best
)
select
    u.signature,
    h.signer,
    u.block_time,
    to_timestamp(u.block_time)::date as date,
    u.market_key,
    u.ticker,
    u.platform,
    u.underlying_mint,
    u.action,
    u.amm_pool,
    u.clmm_orderbook,
    greatest(abs(u.outflow_ui), u.inflow_ui) as notional_underlying,
    -- USD: notional × daily close price. NULL if no price coverage that day.
    greatest(abs(u.outflow_ui), u.inflow_ui) * p.price_usd as notional_usd,
    p.price_usd                       as underlying_price_usd,
    p.source                          as price_source,
    case
        when u.action in ('buyYt', 'sellYt') then 'YT'
        when u.action = 'tradePt'             then 'PT'
        else 'OTHER'
    end as side,
    case
        when u.action = 'buyYt'  then 'buy'
        when u.action = 'sellYt' then 'sell'
        -- tradePt: direction is determined by which side the SIGNER (user) is on.
        --   signer pays underlying  (outflow > inflow) → user BOUGHT PT
        --   signer receives underlying (inflow > outflow) → user SOLD PT
        -- Use signer-only deltas when available; if signer has zero on either
        -- side, fall back to all-party heuristic.
        when u.action = 'tradePt' and abs(u.signer_outflow_ui) > u.signer_inflow_ui then 'buy'
        when u.action = 'tradePt' and u.signer_inflow_ui > abs(u.signer_outflow_ui) then 'sell'
        when u.action = 'tradePt' and abs(u.outflow_ui) > u.inflow_ui                then 'buy'
        when u.action = 'tradePt' and u.inflow_ui > abs(u.outflow_ui)                then 'sell'
        when u.action = 'tradePt'                                                     then 'unknown'
    end as direction
from unioned u
left join {{ ref('stg_prices') }} p
    on  p.mint = u.underlying_mint
    and p.date = to_timestamp(u.block_time)::date
left join {{ ref('stg_helius_tx') }} h
    on h.signature = u.signature
where greatest(abs(u.outflow_ui), u.inflow_ui) > 0
