-- Per-wallet USD holdings across PT + YT + LP in ACTIVE markets only.
-- One row per (wallet, market_key, leg). Active = market.maturity_date >= today.
--
-- Valuation (matches Exponent SDK):
--   PT_USD = pt_balance × pt_price_ratio × underlying_USD
--            pt_price_ratio = e^(-last_ln_implied × years_remaining)
--   YT_USD = yt_balance × yt_price_ratio × underlying_USD
--            yt_price_ratio = 1 − pt_price_ratio  (Pendle invariant)
--   LP_USD = (wallet_lp / total_lp_in_pool) × pool_TVL_USD
--            pool_TVL_USD = sy_balance × sy_rate × underlying_USD
--                         + pt_balance / pt_exchange_rate × underlying_USD
--
-- LP valuation is now per-POOL (a market can have multiple sibling pools;
-- the user's LpPosition.vault tells us which pool). int_pool_reserves_daily
-- carries per-pool financials keyed by pool_account.
{{ config(materialized='table') }}

with mint_legs as (
    select market_key, pt_mint as mint, 'PT' as leg from {{ ref('dim_markets') }} where pt_mint is not null
    union all
    select market_key, yt_mint, 'YT' from {{ ref('dim_markets') }} where yt_mint is not null
),
latest_snap as (
    select max(snapshot_date) as snapshot_date from {{ ref('int_holders_current') }}
),
latest_pos_snap as (
    select max(snapshot_date) as snapshot_date from {{ source('raw', 'raw_positions') }}
),
-- Market pricing from the TIME CURVE (matches SDK market.js
-- currentPtPriceInAsset = e^(last_ln_implied × years_remaining)):
--   pt_price_ratio = 1 / pt_exchange_rate
--   yt_price_ratio = 1 - pt_price_ratio   (Pendle invariant)
--
-- Previously this used int_implied_prices_daily (median observed PT price
-- from AMM trades), which is noisy with low-trade-volume markets and gave
-- wildly off values (e.g. ONyc-10SEP26 with 6 trades/day got pt=0.87 vs
-- time-curve 0.97 → YT overstated ~3×).
--
-- For markets with multiple sibling pools, pick the highest-TVL pool's rate.
canonical_pool as (
    select market_key, pt_exchange_rate, underlying_price_usd,
           row_number() over (partition by market_key order by liquidity_usd desc) as rk
    from {{ ref('int_pool_reserves_daily') }}
    where market_key is not null
),
market_pricing as (
    select
        market_key,
        1.0 / nullif(pt_exchange_rate, 0)                       as pt_price_ratio,
        1.0 - (1.0 / nullif(pt_exchange_rate, 0))               as yt_price_ratio,
        coalesce(underlying_price_usd, 0)                       as underlying_usd
    from canonical_pool
    where rk = 1
),
-- Per-(wallet, market) YT staged interest in raw atoms — from the position
-- account's interest.staged field (decoded by extract_positions at offset
-- 112). Convert to USD via the vault's SY exchange rate × underlying USD.
yt_staged as (
    select
        p.owner                                          as wallet,
        m.market_key,
        sum(coalesce(p.staged_raw, 0))::double           as staged_atoms,
        m.underlying_decimals,
        m.underlying_mint
    from {{ source('raw', 'raw_positions') }} p, latest_pos_snap ls
    join {{ ref('dim_markets') }} m on m.vault = p.vault
    where p.leg = 'YT'
      and p.snapshot_date = ls.snapshot_date
      and m.yt_mint is not null
      and m.maturity_date >= ls.snapshot_date
      and coalesce(m.is_test, false) = false
    group by p.owner, m.market_key, m.underlying_decimals, m.underlying_mint
),
sy_rates as (
    -- Latest SY-to-underlying rate per sy_mint (which == m.sy_mint).
    select sy_mint, rate
    from {{ ref('stg_sy_exchange_rates') }} r
    where r.date = (
        select max(date) from {{ ref('stg_sy_exchange_rates') }} r2
        where r2.sy_mint = r.sy_mint
    )
),
-- Resolved staged-interest USD per (wallet, market) for YT.
yt_staged_usd as (
    select ys.wallet, ys.market_key,
           ys.staged_atoms
             / power(10.0, coalesce(ys.underlying_decimals, 6))
             * coalesce(sr.rate, 1.0)
             * coalesce(mp.underlying_usd, 0)              as staged_usd
    from yt_staged ys
    join {{ ref('dim_markets') }} m on m.market_key = ys.market_key
    left join sy_rates sr on sr.sy_mint = m.sy_mint
    left join market_pricing mp on mp.market_key = ys.market_key
),
-- ─── PT and YT (via int_holders_current) ───────────────────────────────
pt_yt as (
    select
        h.owner                                          as wallet,
        ml.market_key,
        ml.leg,
        m.maturity_date,
        h.amount,
        case
            when ml.leg = 'PT' then h.amount * mp.pt_price_ratio * mp.underlying_usd
            -- YT value = present value of remaining yield (yt × (1 - pt_price))
            --          + staged interest (already accrued, queued for collect).
            -- Unstaged accrued interest (earned since lastSeenIndex, not yet
            -- staged) is a known gap — would require decoding the position's
            -- 256-bit lastSeenIndex + the vault account.
            when ml.leg = 'YT' then
                h.amount * mp.yt_price_ratio * mp.underlying_usd
                + coalesce(ysd.staged_usd, 0)
            else 0
        end::double                                       as usd_value
    from {{ ref('int_holders_current') }} h
    join mint_legs ml on ml.mint = h.mint
    join {{ ref('dim_markets') }} m on m.market_key = ml.market_key
    join latest_snap ls on ls.snapshot_date = h.snapshot_date
    left join market_pricing mp on mp.market_key = ml.market_key
    left join yt_staged_usd ysd on ysd.wallet = h.owner and ysd.market_key = ml.market_key
    where h.amount > 0
      and m.maturity_date >= h.snapshot_date
      and coalesce(m.is_test, false) = false
),
-- ─── LP (via raw_positions, keyed by pool vault) ──────────────────────
-- Sum total LP per pool (= raw_positions.vault for LP positions).
pool_lp_total as (
    select p.vault                            as pool_account,
           sum(p.amount_raw)::double          as total_lp_atoms
    from {{ source('raw', 'raw_positions') }} p, latest_pos_snap ls
    where p.leg = 'LP' and p.amount_raw > 0
      and p.snapshot_date = ls.snapshot_date
    group by p.vault
),
lp_positions as (
    select
        p.owner                               as wallet,
        p.vault                               as pool_account,
        p.amount_raw::double                  as amount_atoms
    from {{ source('raw', 'raw_positions') }} p, latest_pos_snap ls
    where p.leg = 'LP' and p.amount_raw > 0
      and p.snapshot_date = ls.snapshot_date
),
lp as (
    select
        lp.wallet,
        pr.market_key,
        'LP'                                  as leg,
        m.maturity_date,
        lp.amount_atoms / power(10.0, coalesce(m.underlying_decimals, 6)) as amount,
        case when plt.total_lp_atoms > 0
             then lp.amount_atoms / plt.total_lp_atoms * coalesce(pr.liquidity_usd, 0)
             else 0
        end::double                            as usd_value
    from lp_positions lp
    join {{ ref('int_pool_reserves_daily') }} pr on pr.pool_account = lp.pool_account
    join pool_lp_total plt on plt.pool_account = lp.pool_account
    join {{ ref('dim_markets') }} m on m.market_key = pr.market_key
    where pr.market_key is not null
      and m.maturity_date >= current_date
      and coalesce(m.is_test, false) = false
)
select * from pt_yt
union all
select * from lp
