-- Volume-weighted average ENTRY implied yield per (market, holder, leg),
-- plus a market-level average across holders. "Entry" = acquisition trades:
-- PT buys (tradePt, direction=buy) for PT holders, buyYt for YT holders.
-- Each trade's IY is weighted by its underlying notional (cost basis).
--
-- Coverage note: PT-buy execution price resolves ~62% of the time (routed
-- PT), YT ~100%; a holder's average uses whatever priced buys they have.
{{ config(materialized='table') }}

with buys as (
    select market_key, leg, signature, block_time, notional_underlying, entry_iy
    from {{ ref('int_swap_execution_iy') }}
    where direction = 'buy'
),
-- attach the signer (holder) to each buy
buy_with_holder as (
    select b.*, h.signer as holder
    from buys b
    join {{ ref('stg_helius_tx') }} h using (signature)
),
per_holder as (
    select
        market_key, leg, holder,
        sum(entry_iy * notional_underlying) / nullif(sum(notional_underlying), 0) as avg_entry_iy,
        sum(notional_underlying) as entry_notional,
        count(*)                 as n_buys,
        max(block_time)          as last_buy_ts
    from buy_with_holder
    group by 1, 2, 3
)
select
    market_key,
    leg,
    holder,
    avg_entry_iy,
    entry_notional,
    n_buys,
    last_buy_ts,
    -- market-wide notional-weighted average across all holders (same on every
    -- row of a (market, leg) group — the UI reads it once for the headline)
    sum(avg_entry_iy * entry_notional) over (partition by market_key, leg)
        / nullif(sum(entry_notional) over (partition by market_key, leg), 0) as market_avg_entry_iy,
    count(*) over (partition by market_key, leg) as market_priced_holders
from per_holder
