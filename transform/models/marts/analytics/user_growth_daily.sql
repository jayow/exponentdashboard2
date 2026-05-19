-- Daily wallet activity + cohort growth.
--
--   active_wallets   — distinct signers who swapped that day
--   new_wallets      — wallets whose first_seen == that day
--   cumulative_wallets — running total of unique signers seen ≤ that day
--   swaps / volume_usd — supporting per-day totals
{{ config(materialized='table') }}

with per_swap as (
    select date, signer, notional_usd
    from {{ ref('int_amm_swaps') }}
    where signer is not null and date is not null
),
first_seen as (
    select signer, min(date) as first_date from per_swap group by signer
),
per_day as (
    select
        date,
        count(distinct signer)             as active_wallets,
        count(*)                           as swaps,
        sum(notional_usd)                  as volume_usd
    from per_swap
    group by date
),
new_per_day as (
    select first_date as date, count(*) as new_wallets
    from first_seen group by first_date
),
joined as (
    select
        d.date,
        d.active_wallets,
        coalesce(n.new_wallets, 0) as new_wallets,
        d.swaps,
        d.volume_usd
    from per_day d
    left join new_per_day n using (date)
)
select
    date,
    active_wallets,
    new_wallets,
    swaps,
    volume_usd,
    sum(new_wallets) over (order by date)  as cumulative_wallets
from joined
order by date
