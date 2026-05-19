-- Per-wallet lifetime aggregates from indexed swap activity.
--
-- Built from int_amm_swaps (which now carries signer). One row per signer
-- (= the wallet that signed the transaction = the user, for ordinary
-- user-driven swaps; bot/aggregator hot wallets aggregate to themselves
-- which is the right behavior for a "trader leaderboard" view).
{{ config(materialized='table') }}

select
    s.signer,
    count(*)                                                            as n_swaps,
    count(distinct s.market_key)                                        as n_markets,
    count(distinct s.ticker)                                            as n_tickers,
    sum(s.notional_usd)                                                 as total_volume_usd,
    sum(s.notional_underlying)                                          as total_volume_underlying,
    count(*) filter (where s.action = 'buyYt')                          as n_buy_yt,
    count(*) filter (where s.action = 'sellYt')                         as n_sell_yt,
    count(*) filter (where s.action = 'tradePt' and s.direction = 'buy')   as n_buy_pt,
    count(*) filter (where s.action = 'tradePt' and s.direction = 'sell')  as n_sell_pt,
    min(s.date)                                                         as first_seen,
    max(s.date)                                                         as last_seen,
    date_diff('day', min(s.date), max(s.date)) + 1                      as active_span_days
from {{ ref('int_amm_swaps') }} s
where s.signer is not null
group by s.signer
