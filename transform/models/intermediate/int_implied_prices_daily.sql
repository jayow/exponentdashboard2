-- Implied PT and YT prices per (market, date), derived from observed AMM trades.
--
-- No-arbitrage: 1 PT + 1 YT = 1 underlying (since PT redeems for 1 underlying
-- at maturity and YT captures all yield until maturity). So if we observe the
-- PT price from a trade, we get YT price for free as (underlying - PT_price).
--
-- For each AMM swap, we recover the implied PT price by dividing the
-- underlying notional the signer paid (or received) by the PT amount they
-- received (or paid). Median over the day gives us a stable per-market price.
--
-- Forward-fills across days without trades. Useful for valuing outstanding
-- PT/YT supplies at market price rather than face value, which we need to
-- split the "PT-backed SY" TVL bucket into separate Principal (PT) and
-- Farm (YT) sub-buckets.
{{ config(materialized='table') }}

with trades_with_pt_delta as (
    -- For each tradePt/buyYt/sellYt swap, compute the signer's PT delta.
    -- That + notional_underlying gives implied PT price for that trade.
    select
        s.signature,
        s.market_key,
        s.date,
        s.action,
        s.notional_underlying,
        s.notional_usd,
        s.underlying_price_usd,
        m.pt_mint,
        m.yt_mint,
        sum(case when c.owner = h.signer and c.mint = m.pt_mint then c.delta_ui else 0 end) as signer_pt_delta,
        sum(case when c.owner = h.signer and c.mint = m.yt_mint then c.delta_ui else 0 end) as signer_yt_delta
    from {{ ref('int_amm_swaps') }} s
    join {{ ref('dim_markets') }} m using (market_key)
    join {{ ref('stg_helius_tx') }} h on h.signature = s.signature
    join {{ ref('stg_token_changes') }} c on c.signature = s.signature
    where s.notional_underlying > 0 and (m.pt_mint is not null or m.yt_mint is not null)
    group by s.signature, s.market_key, s.date, s.action, s.notional_underlying,
             s.notional_usd, s.underlying_price_usd, m.pt_mint, m.yt_mint
),
per_trade_price as (
    -- Implied PT price = underlying paid / PT received.
    -- For tradePt: signer_pt_delta is the amount the user got (buy) or lost (sell).
    --   PT price = notional_underlying / |signer_pt_delta|
    -- For buyYt: user got YT, paid underlying.
    --   YT price = notional_underlying / |signer_yt_delta|
    --   PT price = underlying - YT price (no-arbitrage)
    -- For sellYt: user gave YT, got underlying.
    --   Same YT price formula → PT price = underlying - YT price.
    select
        market_key, date, action,
        case
            when action = 'tradePt' and abs(signer_pt_delta) > 0
                then notional_underlying / abs(signer_pt_delta)
            when action in ('buyYt', 'sellYt') and abs(signer_yt_delta) > 0
                then 1.0 - (notional_underlying / abs(signer_yt_delta))
        end as implied_pt_price,
        underlying_price_usd
    from trades_with_pt_delta
),
filtered as (
    -- Keep only sane prices (0 < PT < 1). Outliers come from compound txs or
    -- LP-deposit flow misattributed to a trade.
    select market_key, date, implied_pt_price, underlying_price_usd
    from per_trade_price
    where implied_pt_price > 0.05 and implied_pt_price < 1.0
),
daily_observed as (
    select
        market_key, date,
        median(implied_pt_price)     as pt_price_ratio,  -- as fraction of underlying
        median(underlying_price_usd) as underlying_price_usd,
        count(*)                     as n_trades
    from filtered
    group by 1, 2
),
-- Continuous date axis per market, from the market's first PT/YT supply
-- date (NOT the first trade date) so dates before the first observed trade
-- can backward-fill their PT/YT prices. Otherwise tvl_daily falls back to
-- face value (PT=1.0, YT=0.0) for the pre-trade period and YT silently
-- disappears from the historical Breakdown.
bounds as (
    select
        market_key,
        least(
            (select min(date) from main_intermediate.int_mint_supplies_daily s
              where s.market_key = obs.market_key and s.leg in ('PT','YT') and s.supply_ui > 0),
            min(obs.date)
        ) as first_date
    from daily_observed obs
    group by market_key
),
date_axis as (
    select b.market_key, t.date::date as date
    from bounds b,
        unnest(generate_series(b.first_date, current_date, interval 1 day)) as t(date)
    where b.first_date is not null
),
filled as (
    select
        d.market_key, d.date,
        dly.pt_price_ratio,
        dly.underlying_price_usd,
        dly.n_trades
    from date_axis d
    left join daily_observed dly using (market_key, date)
),
-- Forward + backward fill: forward carries last observed price ahead;
-- backward propagates the FIRST observed price to earlier dates so the
-- pre-trade period gets a non-NULL (and non-1.0) PT/YT ratio.
ffilled as (
    select
        f.market_key, f.date,
        coalesce(
            f.pt_price_ratio,
            last_value(f.pt_price_ratio ignore nulls) over (
                partition by f.market_key order by f.date
                rows between unbounded preceding and current row
            )
        ) as ff_pt,
        coalesce(
            f.underlying_price_usd,
            last_value(f.underlying_price_usd ignore nulls) over (
                partition by f.market_key order by f.date
                rows between unbounded preceding and current row
            )
        ) as ff_und,
        f.pt_price_ratio, f.underlying_price_usd, f.n_trades
    from filled f
),
bfilled as (
    select
        ffl.market_key, ffl.date,
        coalesce(
            ffl.ff_pt,
            first_value(ffl.pt_price_ratio ignore nulls) over (
                partition by ffl.market_key order by ffl.date
                rows between current row and unbounded following
            )
        ) as pt_price_ratio,
        coalesce(
            ffl.ff_und,
            first_value(ffl.underlying_price_usd ignore nulls) over (
                partition by ffl.market_key order by ffl.date
                rows between current row and unbounded following
            )
        ) as underlying_price_usd,
        ffl.n_trades
    from ffilled ffl
)
select
    market_key,
    date,
    pt_price_ratio,
    1.0 - pt_price_ratio as yt_price_ratio,
    n_trades,
    underlying_price_usd
from bfilled
where pt_price_ratio is not null
