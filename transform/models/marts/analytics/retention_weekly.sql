-- Weekly retention: per ISO week (Monday-anchored), how many distinct
-- signers swapped that week, split into "new" (first-ever Exponent swap
-- happened this week) vs "returning" (swapped before).
--
-- Matches v1's analytics.json.retention shape — drives the stacked
-- New/Returning bars on the Users tab.
{{ config(materialized='table') }}

with swap_signers as (
    select
        signer,
        date,
        date_trunc('week', date)::date as week  -- ISO week start (Mon)
    from {{ ref('int_amm_swaps') }}
    where signer is not null
),
first_seen as (
    select signer, min(date) as first_date from swap_signers group by signer
),
labeled as (
    select
        s.week,
        s.signer,
        s.date,
        f.first_date,
        date_trunc('week', f.first_date)::date as first_week
    from swap_signers s
    join first_seen f using (signer)
)
select
    week,
    count(distinct signer) filter (where week = first_week) as new_wallets,
    count(distinct signer) filter (where week > first_week) as returning_wallets,
    count(distinct signer)                                    as total_active
from labeled
group by week
order by week
