-- One row per AMM swap. The canonical "trading volume" source.
--
-- notional_underlying = abs(AMM pool's underlying flow) — once per tx.
-- side                = 'PT' | 'YT' — user-intent classification.
--
-- Volume is summed from this table and ONLY this table. If you ever want to
-- change what counts as trading volume, change this model.
{{ config(materialized='table') }}

select
    s.signature,
    s.block_time,
    to_timestamp(s.block_time)::date as date,
    s.market_key,
    e.signer                          as wallet,
    case
        when e.action in ('buyPt', 'sellPt') then 'PT'
        when e.action in ('buyYt', 'sellYt') then 'YT'
        else 'UNKNOWN'
    end                               as side,
    e.action,
    s.pool_address,
    coalesce(s.underlying_in, s.underlying_out) as notional_underlying,
    s.pt_price
from {{ ref('int_amm_swaps') }} s
left join {{ ref('int_classified_events') }} e using (signature)
where s.underlying_in is not null or s.underlying_out is not null
