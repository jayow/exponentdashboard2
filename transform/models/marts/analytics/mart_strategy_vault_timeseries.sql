-- Full daily history per strategy vault — state, APY, and flow columns
-- co-located on one row per (vault, date).
--
-- LEFT JOINs on date so a vault with a state snapshot but no user
-- activity that day still gets a row (flow columns NULL / 0). APY
-- columns are NULL until enough history exists (see int_strategy_vault_apy_daily).
{{ config(materialized='table') }}

select
    s.vault,
    s.snapshot_date                       as date,

    -- State
    s.aum_in_base,
    s.total_aum,
    s.lp_balance,
    s.nav_per_share,
    s.pct_full,
    s.deployed_ratio,

    -- APY
    a.apy_7d,
    a.apy_30d,

    -- Flows (coalesce to 0 for days with no user activity)
    coalesce(f.deposits_lp_delta, 0)      as deposits_lp_delta,
    coalesce(f.execute_lp_delta,  0)      as execute_lp_delta,
    coalesce(f.net_flow_lp,       0)      as net_flow_lp,
    coalesce(f.unique_actors,     0)      as unique_actors
from {{ ref('int_strategy_vault_daily') }} s
left join {{ ref('int_strategy_vault_apy_daily') }} a
  on a.vault         = s.vault
 and a.snapshot_date = s.snapshot_date
left join {{ ref('int_strategy_vault_flows_daily') }} f
  on f.vault       = s.vault
 and f.action_date = s.snapshot_date
