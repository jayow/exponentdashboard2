-- Exact per-swap execution implied yield, from staged token changes (no RPC,
-- no payload re-decode). For every classified swap we already know the
-- underlying notional (int_amm_swaps); here we recover the PT/YT leg amount
-- from stg_token_changes and turn the execution price into an implied APY.
--
-- Execution PT price (PT price in underlying, 0..1):
--   tradePt : notional / |signer's PT delta|      (PT trades on the trader's
--             own account; ~62% coverage — routed PT lands elsewhere)
--   buy/sellYt : 1 − notional / |net YT mint|      (YT is minted/burned into an
--             intermediary, so use the tx-wide net YT supply change; ~100%)
--
-- entry_iy = (1/pt_price)^(365/days_to_maturity_at_trade) − 1
{{ config(materialized='table') }}

with sw as (
    select signature, block_time, market_key, action, notional_underlying
    from {{ ref('int_amm_swaps') }}
    where market_key is not null
      and market_key != 'UNCLASSIFIED'
      and notional_underlying > 0
),
mk as (
    select market_key, pt_mint, yt_mint, maturity_ts, maturity_date
    from {{ ref('stg_markets') }}
),
sig as (
    select signature, signer from {{ ref('stg_helius_tx') }}
),
legs as (
    select
        sw.signature, sw.block_time, sw.market_key, sw.action,
        sw.notional_underlying, m.maturity_ts, m.maturity_date,
        -- trader's own PT delta (direction is unambiguous)
        sum(tc.delta_ui) filter (where tc.mint = m.pt_mint and tc.owner = sig.signer) as signer_pt,
        -- tx-wide net YT supply change (mint on buy, burn on sell)
        sum(tc.delta_ui) filter (where tc.mint = m.yt_mint)                            as net_yt
    from sw
    join mk m using (market_key)
    join sig using (signature)
    left join {{ ref('stg_token_changes') }} tc on tc.signature = sw.signature
    group by 1, 2, 3, 4, 5, 6, 7
),
priced as (
    select
        signature, block_time, market_key, action,
        notional_underlying,
        case
            when action = 'tradePt'            then 'PT'
            when action in ('buyYt', 'sellYt') then 'YT'
        end as leg,
        case
            when action = 'tradePt' then case when signer_pt > 0 then 'buy' else 'sell' end
            when action = 'buyYt'   then 'buy'
            when action = 'sellYt'  then 'sell'
        end as direction,
        case
            when action = 'tradePt' and abs(signer_pt) > 1e-9
                then notional_underlying / abs(signer_pt)
            when action in ('buyYt', 'sellYt') and abs(net_yt) > 1e-9
                then 1.0 - notional_underlying / abs(net_yt)
        end as pt_price_ratio,
        greatest(date_diff('day', to_timestamp(block_time)::date, maturity_date), 1) as days_to_maturity
    from legs
    where maturity_date is not null
)
select
    signature, block_time, market_key, action, leg, direction,
    notional_underlying,
    pt_price_ratio,
    pow(1.0 / pt_price_ratio, 365.0 / days_to_maturity) - 1.0 as entry_iy
from priced
-- Keep only economically sane execution prices (guards fee/rounding noise
-- and the occasional mis-attributed leg).
where pt_price_ratio between 0.5 and 1.0
