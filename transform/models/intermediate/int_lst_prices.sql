-- USD prices for LSTs (and other yield-bearing wrappers) derived from
-- on-chain exchange rates × the base asset's USD price.
--
--   lst_usd = base_usd / lst_per_base
--           = base_usd × (base / lst)
--
-- For each row in raw_lst_rates, find the base mint's USD price on the same
-- date. If the base price is missing we emit NULL (don't fabricate). The
-- ffill/bfill in stg_prices then handles continuity once we DO have it.
--
-- Reads raw_prices DIRECTLY (not stg_prices) to avoid a circular ref —
-- base mints are never LSTs themselves, so their prices come from Jupiter
-- or Pyth and live in raw_prices verbatim.
{{ config(materialized='view') }}

with base_prices as (
    -- One row per (mint, date), picking the highest-priority source.
    select
        mint, date, close_usd, open_usd, high_usd, low_usd,
        row_number() over (
            partition by mint, date
            order by case source
                       when 'pyth'    then 0
                       when 'jupiter' then 1
                       when 'stable'  then 2
                       else 3
                     end
        ) as rk
    from {{ source('raw', 'raw_prices') }}
    where close_usd is not null
)
select
    l.lst_mint                                            as mint,
    l.snapshot_date                                       as date,
    bp.close_usd / l.lst_per_base                         as close_usd,
    bp.open_usd  / nullif(l.lst_per_base, 0)              as open_usd,
    bp.high_usd  / nullif(l.lst_per_base, 0)              as high_usd,
    bp.low_usd   / nullif(l.lst_per_base, 0)              as low_usd,
    0::double                                             as volume_usd,
    'lst_rate'                                            as source,
    l.base_mint                                           as base_mint,
    l.lst_per_base
from {{ source('raw', 'raw_lst_rates') }} l
left join base_prices bp
       on bp.mint = l.base_mint
      and bp.date = l.snapshot_date
      and bp.rk   = 1
where l.lst_per_base > 0
