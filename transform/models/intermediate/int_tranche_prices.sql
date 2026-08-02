-- USD price per tranche LP token (srONyc / jrONyc), derived from on-chain
-- tranche NAV.
--
-- Why this exists: the tranche LP mints are their own "underlying" in
-- dim_markets (srONyc's underlying_mint IS the srONyc mint), and no external
-- venue quotes them — Jupiter/Pyth have nothing. Without a price,
-- int_sy_tvl_daily computes tvl_usd = NULL and tvl_daily's
-- `where sy.tvl_usd > 0` silently drops the market entirely. That was hiding
-- the whole srONyc-10SEP26 market (924,527 SY units, ~$1M of OnRe TVL) from
-- every served number while dq_anomalies only logged it as a "price_gap".
--
--   price = effective_nav_usd / (lp_supply / 10^decimals)
--
-- EFFECTIVE nav, not raw: effective is net of impermanent loss and is what
-- Exponent itself reports. Verified 2026-08-02 against
-- api.exponent.finance/markets — srONyc syExchangeRate 1.009649832466 vs
-- this model's 1.009649832 (9 dp). Raw NAV gives 1.010895705, which is wrong.
--
-- Emitted at lowest price priority ('tranche_nav') in stg_prices, so if a
-- real quote ever appears for these mints it wins. stg_prices forward-fills,
-- which matters here: extract_tranche_states only snapshots on run days.
{{ config(materialized='view') }}

with states as (
    select
        snapshot_date as date,
        mint_lp_senior,
        mint_lp_junior,
        sr_effective_nav_usd,
        jr_effective_nav_usd,
        total_sr_lp_supply,
        total_jr_lp_supply
    from {{ source('raw', 'raw_tranche_states') }}
),
legs as (
    select date, mint_lp_senior as mint, sr_effective_nav_usd as nav_usd,
           total_sr_lp_supply as lp_supply_raw
    from states
    union all
    select date, mint_lp_junior, jr_effective_nav_usd, total_jr_lp_supply
    from states
),
priced as (
    select
        l.date,
        l.mint,
        -- Both ONyc tranche LP mints are 9dp; coalesce keeps a future tranche
        -- with different decimals from silently mispricing by 10^n.
        l.nav_usd / (l.lp_supply_raw / power(10, coalesce(tm.decimals, 9))) as close_usd
    from legs l
    left join {{ source('raw', 'raw_token_metadata') }} tm on tm.mint = l.mint
    where l.mint is not null
      and l.nav_usd > 0
      and l.lp_supply_raw > 0
)
select
    mint,
    date,
    close_usd,
    close_usd as open_usd,
    close_usd as high_usd,
    close_usd as low_usd,
    cast(null as double) as volume_usd,
    'tranche_nav' as source
from priced
