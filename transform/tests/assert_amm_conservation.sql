-- Custom data test: every swap's AMM legs must roughly conserve value.
-- underlying_in × 1 ≈ pt_in × pt_price (within 1% tolerance for fees/rounding).
-- Returns failing rows; dbt test passes iff zero rows returned.

with checked as (
    select
        signature,
        coalesce(underlying_in, underlying_out) as underlying_leg,
        coalesce(pt_in, pt_out)                  as pt_leg,
        pt_price
    from {{ ref('fct_swaps') }}
    where pt_price is not null and pt_price > 0
)
select *
from checked
where abs(underlying_leg - pt_leg * pt_price) / nullif(underlying_leg, 0) > 0.01
