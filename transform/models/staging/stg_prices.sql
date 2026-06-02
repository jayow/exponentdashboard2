-- Per-(mint, date) USD price. Daily granularity.
--
-- Source priority: pyth > jupiter > stable (1.0 fallback for pegged stables).
--
-- Fill strategy:
--   * Forward-fill: if a mint has price on D but not D+1, carry D's value
--     forward (LSTs barely change minute-to-minute; avoids "no TVL today
--     because extract_prices hasn't run yet").
--   * Backward-fill: for dates BEFORE the first observed price (typically
--     when a token existed on-chain but Jupiter/Pyth hadn't started covering
--     it yet), carry the FIRST observed price backward. This prevents the
--     classic "TVL jumps from $0 to $X when pricing coverage starts" bug.
--     Assumption: pre-coverage price ≈ first-observed price. For most LSTs
--     and stables this is within a few % — acceptable for historical view.
--
-- Date axis extends from min(first_change_date, first_price_date) so we
-- backfill any period where a mint had on-chain activity but no price yet.
{{ config(materialized='view') }}

with all_prices as (
    select mint, date, close_usd, open_usd, high_usd, low_usd, volume_usd, source
    from {{ source('raw', 'raw_prices') }}
    union all
    -- LST prices derived on-chain from raw_lst_rates × base price.
    -- Lower priority than direct Jupiter/Pyth (source='lst_rate').
    select mint, date, close_usd, open_usd, high_usd, low_usd, volume_usd, source
    from {{ ref('int_lst_prices') }}
    where close_usd is not null
),
ranked as (
    select
        mint, date, close_usd, open_usd, high_usd, low_usd, volume_usd, source,
        row_number() over (
            partition by mint, date
            order by case source
                       when 'pyth'     then 0
                       when 'jupiter'  then 1
                       when 'stable'   then 2
                       when 'lst_rate' then 3
                       else 4 end
        ) as rk
    from all_prices
),
canonical as (
    select mint, date, close_usd as price_usd, open_usd, high_usd, low_usd, volume_usd, source
    from ranked where rk = 1 and close_usd is not null
),
-- For each mint we need prices on every date from (first activity OR
-- first price) through today. "Activity" = first appearance in
-- stg_token_changes (the mint shows up on-chain).
mint_first_activity as (
    select mint, min(to_timestamp(block_time)::date) as first_activity
    from {{ ref('stg_token_changes') }}
    group by mint
),
mint_first_price as (
    select mint, min(date) as first_price
    from canonical group by mint
),
bounds as (
    select
        coalesce(p.mint, a.mint) as mint,
        least(coalesce(a.first_activity, p.first_price), coalesce(p.first_price, a.first_activity)) as first_date
    from mint_first_price p
    full outer join mint_first_activity a using (mint)
    where coalesce(a.first_activity, p.first_price) is not null
),
date_axis as (
    select b.mint, t.date::date as date
    from bounds b,
        unnest(generate_series(b.first_date, current_date, interval 1 day)) as t(date)
),
joined as (
    select
        d.mint, d.date,
        c.price_usd, c.open_usd, c.high_usd, c.low_usd, c.volume_usd, c.source
    from date_axis d
    left join canonical c on c.mint = d.mint and c.date = d.date
),
-- Forward-fill: carry last non-null price forward
ffilled as (
    select
        j.mint, j.date,
        last_value(j.price_usd ignore nulls) over (
            partition by j.mint order by j.date rows between unbounded preceding and current row
        ) as ff_price,
        last_value(j.source ignore nulls) over (
            partition by j.mint order by j.date rows between unbounded preceding and current row
        ) as ff_source,
        j.price_usd, j.open_usd, j.high_usd, j.low_usd, j.volume_usd, j.source
    from joined j
),
-- Backward-fill: for early dates where forward-fill found no prior value,
-- take the FIRST non-null price seen on or after this date.
bfilled as (
    select
        f.mint, f.date,
        coalesce(
            f.ff_price,
            first_value(f.price_usd ignore nulls) over (
                partition by f.mint order by f.date rows between current row and unbounded following
            )
        ) as price_usd,
        coalesce(
            f.ff_source || (case when f.source is null and f.ff_price is not null then '_ffill' else '' end),
            first_value(f.source ignore nulls) over (
                partition by f.mint order by f.date rows between current row and unbounded following
            ) || '_bfill'
        ) as source,
        f.open_usd, f.high_usd, f.low_usd, f.volume_usd
    from ffilled f
)
select
    mint, date,
    price_usd, open_usd, high_usd, low_usd, volume_usd, source
from bfilled
where price_usd is not null
