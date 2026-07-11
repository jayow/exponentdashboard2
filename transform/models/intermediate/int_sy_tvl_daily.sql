-- DefiLlama-style TVL: one row per (sy_mint, date).
--
-- Mirrors https://github.com/DefiLlama/DefiLlama-Adapters/blob/main/projects/exponent/index.js:
--   amount = SY_supply × SY_exchange_rate
--   tvl_usd = amount × underlying_USD
--
-- SY mints are shared across multiple maturities of the same ticker (one
-- Exponent vault per SY token, many markets per vault). Deduping at the
-- sy_mint level is the only way to avoid 4-6× double-counting per market.
--
-- SY exchange rate is derived empirically from observed deposit txs (see
-- stg_sy_exchange_rates). On Exponent it's ≡ 1.0 across every SY mint; we
-- still flow it through as data so any future rebasing SY is auto-detected.
{{ config(materialized='table') }}

with recon as (
    -- Reconstructed supply (cumulative tx-delta sum). One row per (sy_mint,
    -- date). Carries the underlying_mint mapping + full history. Its absolute
    -- level DRIFTS (misses mint/burn events — USX SY was 31% low), so we only
    -- keep it for the underlying mapping and for dates without an
    -- authoritative snapshot.
    select
        s.mint                                       as sy_mint,
        s.date,
        max(s.supply_ui)                             as recon_supply,
        max(s.underlying_mint)                       as underlying_mint,
        max(s.underlying_decimals)                   as underlying_decimals
    from {{ ref('int_mint_supplies_daily') }} s
    where s.leg = 'SY'
    group by 1, 2
),
authoritative as (
    -- On-chain SPL Mint supply (extract_mint_supplies) — ground truth for the
    -- dates we've snapshotted (latest onward; accumulates going forward).
    select mint as sy_mint, snapshot_date as date, supply_ui
    from {{ source('raw', 'raw_mint_supplies') }}
    where leg = 'SY'
),
derived_hist as (
    -- Derivable-correct daily supply for the recent window that the
    -- reconstruction drifted on (extract_sy_supply_history, one-time backfill).
    select mint as sy_mint, date, supply_ui
    from {{ source('raw', 'raw_sy_supply_history') }}
),
sy_supply as (
    -- Supply precedence: authoritative snapshot (current/forward) > derived
    -- history (recent drifted window) > reconstruction (correct pre-drift
    -- history). Pricing unchanged downstream (rate × underlying_price) —
    -- verified on USX (rate 1.0) and ONyc (accrual is in the price; do NOT
    -- also apply the vault rate, or accruing wrappers double-count).
    select
        r.sy_mint,
        r.date,
        coalesce(a.supply_ui, h.supply_ui, r.recon_supply) as sy_supply_ui,
        r.underlying_mint,
        r.underlying_decimals
    from recon r
    left join authoritative a on a.sy_mint = r.sy_mint and a.date = r.date
    left join derived_hist  h on h.sy_mint = r.sy_mint and h.date = r.date
)
select
    s.date,
    s.sy_mint,
    s.underlying_mint,
    s.sy_supply_ui                                   as sy_supply,
    coalesce(r.rate, 1.0)                            as sy_exchange_rate,
    s.sy_supply_ui * coalesce(r.rate, 1.0)           as tvl_underlying,
    s.sy_supply_ui * coalesce(r.rate, 1.0) * p.price_usd as tvl_usd,
    p.price_usd                                      as underlying_price_usd,
    p.source                                         as price_source,
    r.n_events                                       as rate_n_events
from sy_supply s
-- coalesce(underlying_mint, sy_mint): for LST-style markets where the SY
-- IS the underlying (kUSDG, kUSDC, syrupUSDC, etc.), the dim_markets row
-- has underlying_mint=null while the price flows through stg_prices keyed
-- by sy_mint (via int_lst_prices or jupiter). Falling back to sy_mint here
-- prevents these markets from being priced as $0.
left join {{ ref('stg_prices') }} p
    on p.mint = coalesce(s.underlying_mint, s.sy_mint) and p.date = s.date
left join {{ ref('stg_sy_exchange_rates') }} r
    on r.sy_mint = s.sy_mint and r.date = s.date
where s.sy_supply_ui > 0
