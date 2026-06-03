-- Per-wallet markets with outstanding unclaimed yield, in underlying tokens
-- and USD.
--
-- Two sources of unclaimed yield per (wallet, market):
--   1. `staged_atoms` — already moved from the vault's uncollected_sy pool to
--      the position's staged buffer (collect-able with a single ix). Read
--      directly from raw_positions.staged_raw (YieldTokenPosition.interest.staged
--      at offset 112..120).
--   2. `unstaged_atoms` — accrued in the vault but not yet staged to the
--      position. Computed via the canonical formula from
--      exponent-core/state/yield_token_position.rs::calc_earned_sy:
--          delta = 1/lsi − 1/final_sy_rate
--          unstaged_atoms = floor(delta × yt_balance)
--      where:
--          lsi            = position.interest.last_seen_index (raw_positions.last_seen_index)
--          final_sy_rate  = vault.final_sy_exchange_rate     (raw_vault_states.final_sy_exchange_rate)
--          yt_balance     = position raw atoms                (raw_positions.amount_raw)
--      Both indices are 256-bit fixed-point Numbers; we store them as
--      DOUBLE (U256/1e12, natural value).
--
-- Result is in *YT/underlying atoms* (= same scale as the underlying mint
-- decimals, since Exponent SY mints are 1:1 wrappers — the SY-rate side of
-- the conversion is folded into the lsi/final delta itself).
--
-- A wallet appears here when it currently holds YT AND has any unclaimed
-- yield (staged OR unstaged > 0). We drop the historic "never-claimed" gate
-- — it was an approximation that excluded recent partial claimers.
{{ config(materialized='view') }}

with latest_pos_snap as (
    select max(snapshot_date) as snapshot_date from {{ source('raw', 'raw_positions') }}
),
latest_vault_snap as (
    select max(snapshot_date) as snapshot_date from {{ source('raw', 'raw_vault_states') }}
),
positions as (
    select p.owner as wallet,
           m.market_key,
           p.vault,
           p.amount_raw                       as yt_atoms,
           coalesce(p.staged_raw, 0)          as staged_atoms,
           p.last_seen_index                  as lsi,
           m.underlying_decimals              as decimals,
           m.underlying_mint
    from {{ source('raw', 'raw_positions') }} p
    join latest_pos_snap ls on p.snapshot_date = ls.snapshot_date
    join {{ ref('dim_markets') }} m on m.vault = p.vault
    where p.leg = 'YT' and p.amount_raw > 0
),
vault_rates as (
    select v.vault, v.final_sy_exchange_rate as final_rate
    from {{ source('raw', 'raw_vault_states') }} v
    join latest_vault_snap vs on v.snapshot_date = vs.snapshot_date
),
und_prices as (
    select mint as und_mint, price_usd
    from {{ ref('stg_prices') }} p
    where p.date = (
        select max(date) from {{ ref('stg_prices') }} p2 where p2.mint = p.mint
    )
),
combined as (
    select
        p.wallet,
        p.market_key,
        p.yt_atoms,
        p.staged_atoms,
        p.lsi,
        coalesce(v.final_rate, 0)                                as final_rate,
        p.decimals,
        p.underlying_mint,
        case
            when p.lsi is null or p.lsi <= 0 then 0
            when v.final_rate is null or v.final_rate <= p.lsi then 0
            else floor((1.0/p.lsi - 1.0/v.final_rate) * p.yt_atoms)
        end                                                       as unstaged_atoms
    from positions p
    left join vault_rates v on v.vault = p.vault
),
-- ─── CLMM LP fees ─────────────────────────────────────────────────────
-- Aggregate per (wallet, clmm_market) and resolve market_key by joining
-- through raw_market_three_states.mint_pt → dim_markets.pt_mint
-- (dim_markets.amm_pool stores the Core AMM pool, not the CLMM pubkey).
clmm_fees as (
    select
        u.wallet,
        m.market_key,
        m.underlying_decimals,
        m.underlying_mint,
        sum(u.claim_pt_atoms + u.claim_sy_atoms) as clmm_atoms
    from {{ ref('int_clmm_lp_unclaimed') }} u
    join {{ source('raw', 'raw_market_three_states') }} ms
        on ms.clmm_market = u.clmm_market
       and ms.snapshot_date = (select max(snapshot_date) from {{ source('raw', 'raw_market_three_states') }})
    join {{ ref('dim_markets') }} m on m.pt_mint = ms.mint_pt
    group by u.wallet, m.market_key, m.underlying_decimals, m.underlying_mint
),
yt_per_wallet as (
    select c.wallet, c.market_key,
           sum(c.yt_atoms)        as yt_atoms,
           sum(c.staged_atoms)    as staged_atoms,
           sum(c.unstaged_atoms)  as unstaged_atoms,
           any_value(c.decimals)  as decimals,
           any_value(c.underlying_mint) as underlying_mint
    from combined c
    group by c.wallet, c.market_key
),
merged as (
    select
        coalesce(y.wallet, c.wallet)             as wallet,
        coalesce(y.market_key, c.market_key)     as market_key,
        coalesce(y.decimals, c.underlying_decimals) as decimals,
        coalesce(y.underlying_mint, c.underlying_mint) as underlying_mint,
        coalesce(y.yt_atoms, 0)                  as yt_atoms,
        coalesce(y.staged_atoms, 0)              as staged_atoms,
        coalesce(y.unstaged_atoms, 0)            as unstaged_atoms,
        coalesce(c.clmm_atoms, 0)                as clmm_atoms
    from yt_per_wallet y
    full outer join clmm_fees c
        on c.wallet = y.wallet and c.market_key = y.market_key
)
select
    m.wallet,
    m.market_key,
    m.yt_atoms / power(10.0, coalesce(m.decimals, 6))                    as yt_balance,
    m.staged_atoms                                                       as staged_atoms,
    m.unstaged_atoms                                                     as unstaged_atoms,
    m.clmm_atoms                                                         as clmm_atoms,
    m.staged_atoms   / power(10.0, coalesce(m.decimals, 6))              as staged_underlying,
    m.unstaged_atoms / power(10.0, coalesce(m.decimals, 6))              as unstaged_underlying,
    m.clmm_atoms     / power(10.0, coalesce(m.decimals, 6))              as clmm_underlying,
    (m.staged_atoms + m.unstaged_atoms + m.clmm_atoms)
        / power(10.0, coalesce(m.decimals, 6))                           as unclaimed_underlying,
    (m.staged_atoms + m.unstaged_atoms + m.clmm_atoms)
        / power(10.0, coalesce(m.decimals, 6))
        * coalesce(p.price_usd, 0)                                       as unclaimed_usd
from merged m
left join und_prices p on p.und_mint = m.underlying_mint
where m.staged_atoms + m.unstaged_atoms + m.clmm_atoms > 0
