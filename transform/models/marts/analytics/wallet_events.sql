-- Per-wallet event timeline. One row per (signer, signature) tagged with
-- the user-facing action, the resolved market, USD notional and the
-- signer's own token deltas (as JSON for the UI).
--
-- Sources:
--   int_classified_events  — action vocab (all types, not just trades)
--   stg_helius_tx          — signer
--   int_amm_swaps          — market_key + usd_value for trade actions
--   stg_token_changes      — per-wallet deltas (filtered to signer's own
--                            token accounts) joined back per tx
{{ config(materialized='table') }}

with cls as (
    select signature, block_time, action
    from {{ ref('int_classified_events') }}
    where action <> 'unknown' and action <> 'admin'
),
tx_signer as (
    select signature, signer
    from {{ ref('stg_helius_tx') }}
    where signer is not null
),
-- USD + market_key from int_amm_swaps when the tx is a trade. Other action
-- types (claimYield, addLiq, redeemPt, strip, merge, deposit, withdraw)
-- don't carry a notional in int_amm_swaps; market is resolved via the
-- wallet's own PT/YT/LP/SY deltas below.
trade_meta as (
    select signature, market_key, ticker, notional_usd
    from {{ ref('int_amm_swaps') }}
),
-- For non-trade actions, find market via signer's mint deltas
nontrade_market as (
    select c.signature,
           any_value(m.market_key)   as market_key,
           any_value(m.ticker)       as ticker
    from cls c
    join tx_signer s using (signature)
    join {{ ref('stg_token_changes') }} ch
        on ch.signature = c.signature and ch.owner = s.signer
    join {{ ref('stg_markets') }} m
        on (ch.mint = m.pt_mint or ch.mint = m.yt_mint
            or ch.mint = m.lp_mint or ch.mint = m.sy_mint)
    where c.action not in ('buyYt','sellYt','tradePt')
    group by c.signature
)
select
    c.signature,
    s.signer,
    c.block_time,
    to_timestamp(c.block_time)::date                  as date,
    c.action,
    coalesce(t.market_key, nt.market_key)             as market_key,
    coalesce(t.ticker, nt.ticker)                     as ticker,
    t.notional_usd                                    as usd_value
from cls c
join tx_signer s using (signature)
left join trade_meta t using (signature)
left join nontrade_market nt using (signature)
