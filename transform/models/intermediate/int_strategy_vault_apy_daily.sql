-- Rolling APY per strategy vault, derived from nav_per_share deltas.
--
-- Formula (mirrors int_tranche_apy_daily's sy_exchange_rate approach):
--
--   apy_Nd = (nav_t / nav_{t-N})^(365 / dt_days) - 1
--
-- where nav_t is nav_per_share on snapshot_date, nav_{t-N} is nav_per_share
-- on the row N snapshots back (within the same vault partition), and
-- dt_days is the actual day-gap between those two snapshots (robust to
-- missing snapshot days).
--
-- apy_7d needs at least 8 rows in the partition (t and t-7); apy_30d
-- needs 31. Returns NULL when the lag row doesn't exist yet.
{{ config(materialized='view') }}

with daily as (
    select
        vault,
        snapshot_date,
        nav_per_share
    from {{ ref('stg_strategy_vault_states') }}
    where nav_per_share is not null
),
windowed as (
    select
        vault,
        snapshot_date,
        nav_per_share,
        lag(nav_per_share, 7)  over (partition by vault order by snapshot_date) as nav_7d_ago,
        lag(snapshot_date, 7)  over (partition by vault order by snapshot_date) as date_7d_ago,
        lag(nav_per_share, 30) over (partition by vault order by snapshot_date) as nav_30d_ago,
        lag(snapshot_date, 30) over (partition by vault order by snapshot_date) as date_30d_ago
    from daily
)
select
    vault,
    snapshot_date,
    nav_per_share,

    case
        when nav_7d_ago is not null
         and nav_7d_ago > 0
         and date_7d_ago is not null
         and date_diff('day', date_7d_ago, snapshot_date) > 0
        then pow(
                nav_per_share / nav_7d_ago,
                365.0 / date_diff('day', date_7d_ago, snapshot_date)
             ) - 1
    end as apy_7d,

    case
        when nav_30d_ago is not null
         and nav_30d_ago > 0
         and date_30d_ago is not null
         and date_diff('day', date_30d_ago, snapshot_date) > 0
        then pow(
                nav_per_share / nav_30d_ago,
                365.0 / date_diff('day', date_30d_ago, snapshot_date)
             ) - 1
    end as apy_30d
from windowed
