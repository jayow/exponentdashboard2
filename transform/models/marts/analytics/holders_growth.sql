-- Unique-holder count over time, per market and protocol-wide.
-- Phase 3: derive from fct_swaps + fct_lp_events (first-seen / last-seen per wallet).
{{ config(materialized='table') }}

select
    cast(null as date)    as date,
    cast(null as varchar) as market_key,
    cast(null as bigint)  as holders
where false
