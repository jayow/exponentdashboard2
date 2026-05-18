-- Symbol-resolve every market by parsing the SY token's Metaplex name.
--
-- Pattern: "Exponent Wrapped <Ticker>" → ticker = <Ticker>
-- Partner tokens (kUSDC, mUSDC, marginfi USDC, etc.) — symbol is the ticker.
--
-- Then cross-reference ticker to raw_exponent_tokens (Exponent's /tokens
-- endpoint) to find the actual underlying mint + decimals.
{{ config(materialized='view') }}

with onchain_markets as (
    select
        market_key,
        payload->>'$.syMint'         as sy_mint,
        payload->>'$.vault'          as vault,
        cast(payload->>'$.maturityTs' as bigint) as maturity_ts,
        cast(payload->>'$.maturityDate' as date) as maturity_date,
        fetched_at
    from {{ source('raw', 'raw_markets') }}
    where source = 'onchain'
),
-- SY mint metadata from DAS
sy_meta as (
    select
        mint as sy_mint,
        name as sy_name,
        symbol as sy_symbol,
        decimals as sy_decimals
    from {{ source('raw', 'raw_token_metadata') }}
),
-- Parse ticker from SY name. Three cases:
--   1. "Exponent Wrapped X"           -> ticker = X
--   2. "Kamino USDC JLP Market"       -> ticker = sy_symbol (partner: kUSDC)
--   3. anything else                  -> ticker = sy_symbol or NULL
resolved_ticker as (
    select
        m.market_key,
        m.sy_mint,
        m.vault,
        m.maturity_ts,
        m.maturity_date,
        sm.sy_name,
        sm.sy_symbol,
        sm.sy_decimals,
        case
            when sm.sy_name like 'Exponent Wrapped %'
                then trim(substr(sm.sy_name, length('Exponent Wrapped ') + 1))
            else sm.sy_symbol
        end as resolved_ticker
    from onchain_markets m
    left join sy_meta sm using (sy_mint)
)
select
    r.market_key                                          as raw_market_key,
    r.sy_mint,
    r.vault,
    r.maturity_ts,
    r.maturity_date,
    r.resolved_ticker,
    -- Build a clean market_key with the resolved ticker, replacing the
    -- "UNKNOWN-" prefix when we now know the ticker.
    case
        when r.resolved_ticker is not null
         and r.market_key like 'UNKNOWN-%'
            then replace(r.market_key, 'UNKNOWN', r.resolved_ticker)
        else r.market_key
    end                                                   as market_key,
    et.mint                                               as underlying_mint,
    et.decimals                                           as underlying_decimals,
    et.symbol                                             as underlying_symbol,
    et.name                                               as underlying_name,
    r.sy_name,
    r.sy_symbol,
    r.sy_decimals
from resolved_ticker r
left join {{ source('raw', 'raw_exponent_tokens') }} et
    on lower(et.symbol) = lower(r.resolved_ticker)
