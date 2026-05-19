-- Derive markets from PT/YT/LP mint Metaplex names.
-- Pattern: "PT-<TICKER>-<DDMonYY>", "YT-<TICKER>-<DDMonYY>", or
--          "LP-<TICKER>-<DDMonYY>" (sometimes prefixed with "Exponent ").
--
-- Each PT mint's name encodes the full market identity. For expired markets
-- where our on-chain MarketThree decoder only captured sy_mint (not pt/yt),
-- this gives us pt_mint AND yt_mint AND lp_mint AND ticker AND maturity
-- directly. The Exponent API does NOT return lpMint, so this is our only
-- on-chain-derivable source for LP mint addresses.
--
-- Cross-reference ticker → raw_exponent_tokens for underlying mint.
{{ config(materialized='view') }}

with pt_yt_meta as (
    select
        mint,
        name,
        symbol,
        decimals,
        -- Strip optional "Exponent " prefix
        case
            when name like 'Exponent %' then substr(name, length('Exponent ') + 1)
            else name
        end as clean_name
    from {{ source('raw', 'raw_token_metadata') }}
    where name like 'PT-%' or name like 'YT-%' or name like 'LP-%'
       or name like 'Exponent PT-%' or name like 'Exponent YT-%' or name like 'Exponent LP-%'
),
parsed as (
    select
        mint,
        symbol,
        decimals,
        case
            when clean_name like 'PT-%' then 'PT'
            when clean_name like 'YT-%' then 'YT'
            when clean_name like 'LP-%' then 'LP'
        end                                                     as side,
        -- Extract ticker and date from "PT-TICKER-DDMonYY" / "YT-..." / "LP-..."
        regexp_extract(clean_name, '^(?:PT|YT|LP)-(.+)-(\d{2}[A-Z]{3}\d{2})$', 1) as ticker_raw,
        regexp_extract(clean_name, '^(?:PT|YT|LP)-(.+)-(\d{2}[A-Z]{3}\d{2})$', 2) as maturity_str
    from pt_yt_meta
    where regexp_extract(clean_name, '^(?:PT|YT|LP)-(.+)-(\d{2}[A-Z]{3}\d{2})$', 1) <> ''
),
-- Pivot PT/YT/LP into one row per (ticker, maturity_str)
markets as (
    select
        ticker_raw                              as ticker,
        maturity_str,
        max(case when side = 'PT' then mint end) as pt_mint,
        max(case when side = 'YT' then mint end) as yt_mint,
        max(case when side = 'LP' then mint end) as lp_mint,
        max(decimals)                            as sy_decimals
    from parsed
    group by ticker_raw, maturity_str
)
select
    -- Use the canonical symbol casing from /tokens when available (e.g.
    -- "BulkSOL" not the Metaplex-name's "bulkSOL"). Falls back to the
    -- extracted ticker if no /tokens match.
    coalesce(et.symbol, m.ticker)                                 as ticker,
    coalesce(et.symbol, m.ticker) || '-' || m.maturity_str        as market_key,
    m.maturity_str,
    try_strptime(m.maturity_str, '%d%b%y')::date                  as maturity_date,
    m.pt_mint,
    m.yt_mint,
    m.lp_mint,
    et.mint                                                       as underlying_mint,
    et.decimals                                                   as underlying_decimals,
    et.symbol                                                     as underlying_symbol,
    et.name                                                       as underlying_name
from markets m
left join {{ source('raw', 'raw_exponent_tokens') }} et
    on lower(et.symbol) = lower(m.ticker)
    or (lower(m.ticker) like 'w%' and lower(et.symbol) = lower(substr(m.ticker, 2)))
