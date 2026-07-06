-- Per-day senior/junior/underlying APY for each tranche vault.
--
-- Formulas (derived from Exponent SDK, validated to ~0.06% vs API):
--
--   underlying_apy = (rate_t / rate_{t-N})^(YEAR / dt_seconds) - 1
--   senior_apy    = underlying_apy * (1 - current_jr_return_share)
--   junior_apy    = underlying_apy * (1 + current_jr_return_share
--                                       * sr_effective_nav / jr_effective_nav)
--
-- Underlying APY is NULL until we have ≥2 daily snapshots of the pool's
-- current_sy_exchange_rate (first snapshot: 2026-06-25). Once tomorrow's
-- cron lands, sr/jr APY columns populate automatically.
--
-- LP-price columns are derived here (not on the vault account) — they're
-- effective_nav / total_lp_supply (lp supply uses 9-decimal scale).
{{ config(materialized='view') }}

with state as (
    select
        t.snapshot_date,
        t.tranche_vault,
        t.seed_id,
        t.sy_mint,
        t.sr_raw_nav_usd,
        t.jr_raw_nav_usd,
        t.sr_effective_nav_usd,
        t.jr_effective_nav_usd,
        t.current_junior_return_share,
        t.tw_junior_return_share_accrued,
        t.total_sr_lp_supply,
        t.total_jr_lp_supply,
        t.utilization,
        t.min_coverage_ratio,
        t.last_sync_ts,
        t.fixed_term_end_ts,
        t.fixed_term_duration_sec
    from {{ source('raw', 'raw_tranche_states') }} t
),
pool_rates as (
    select
        snapshot_date,
        sy_mint,
        any_value(current_sy_exchange_rate) as sy_exchange_rate
    from {{ source('raw', 'raw_pool_state') }}
    where current_sy_exchange_rate is not null
    group by 1, 2
),
joined as (
    select
        s.*,
        p.sy_exchange_rate
    from state s
    left join pool_rates p
      on p.snapshot_date = s.snapshot_date
     and p.sy_mint       = s.sy_mint
),
windowed as (
    select
        j.*,
        lag(sy_exchange_rate) over (
            partition by tranche_vault
            order by snapshot_date
        ) as sy_exchange_rate_prev,
        lag(snapshot_date) over (
            partition by tranche_vault
            order by snapshot_date
        ) as snapshot_date_prev
    from joined j
)
select
    snapshot_date,
    tranche_vault,
    seed_id,
    sy_mint,

    -- NAV
    sr_raw_nav_usd,
    jr_raw_nav_usd,
    sr_effective_nav_usd,
    jr_effective_nav_usd,
    sr_raw_nav_usd + jr_raw_nav_usd            as total_nav_usd,
    sr_effective_nav_usd + jr_effective_nav_usd as total_effective_nav_usd,
    case
        when (sr_effective_nav_usd + jr_effective_nav_usd) > 0
            then jr_effective_nav_usd / (sr_effective_nav_usd + jr_effective_nav_usd)
    end                                         as coverage_ratio,
    utilization * 100                           as utilization_pct,
    min_coverage_ratio,

    -- LP supply + lp prices
    total_sr_lp_supply,
    total_jr_lp_supply,
    case when total_sr_lp_supply > 0
         then sr_effective_nav_usd / (total_sr_lp_supply / 1e9)
    end                                         as sr_lp_price,
    case when total_jr_lp_supply > 0
         then jr_effective_nav_usd / (total_jr_lp_supply / 1e9)
    end                                         as jr_lp_price,

    -- Yield + share state
    current_junior_return_share,
    tw_junior_return_share_accrued,
    sy_exchange_rate                            as current_sy_exchange_rate,

    -- Underlying APY (NULL until ≥2 snapshots — first available 2026-06-26)
    case
        when sy_exchange_rate is not null
         and sy_exchange_rate_prev is not null
         and sy_exchange_rate_prev > 0
         and snapshot_date_prev is not null
         and date_diff('day', snapshot_date_prev, snapshot_date) > 0
        then pow(
                sy_exchange_rate / sy_exchange_rate_prev,
                365.0 / date_diff('day', snapshot_date_prev, snapshot_date)
             ) - 1
    end                                         as underlying_apy,

    -- Senior + junior APY — instantaneous formula validated against API
    case
        when sy_exchange_rate is not null
         and sy_exchange_rate_prev is not null
         and sy_exchange_rate_prev > 0
         and snapshot_date_prev is not null
         and date_diff('day', snapshot_date_prev, snapshot_date) > 0
        then (
                pow(
                    sy_exchange_rate / sy_exchange_rate_prev,
                    365.0 / date_diff('day', snapshot_date_prev, snapshot_date)
                ) - 1
             ) * (1 - current_junior_return_share)
    end                                         as senior_apy,

    case
        when sy_exchange_rate is not null
         and sy_exchange_rate_prev is not null
         and sy_exchange_rate_prev > 0
         and snapshot_date_prev is not null
         and date_diff('day', snapshot_date_prev, snapshot_date) > 0
         and jr_effective_nav_usd > 0
        then (
                pow(
                    sy_exchange_rate / sy_exchange_rate_prev,
                    365.0 / date_diff('day', snapshot_date_prev, snapshot_date)
                ) - 1
             ) * (
                1.0
                + current_junior_return_share
                  * sr_effective_nav_usd / jr_effective_nav_usd
             )
    end                                         as junior_apy,

    -- Lifecycle
    last_sync_ts,
    fixed_term_end_ts,
    fixed_term_duration_sec

from windowed
