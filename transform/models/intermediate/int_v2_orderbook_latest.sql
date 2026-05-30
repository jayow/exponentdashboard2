-- Latest snapshot of v2 orders per book. Used by both summary + per-market
-- ladder marts so they read consistent data even if a mid-run extract
-- adds rows for tomorrow's date.
{{ config(materialized='view') }}

with latest as (
    select max(snapshot_date) as snapshot_date
    from {{ source('raw', 'raw_v2_orders_snapshots') }}
)
select o.*
from {{ ref('stg_v2_orders') }} o
join latest l on l.snapshot_date = o.snapshot_date
