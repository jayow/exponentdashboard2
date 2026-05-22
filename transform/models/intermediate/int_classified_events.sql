-- Classify each Exponent tx by user-facing action.
-- Source of truth is the Exponent `Instruction: <name>` log line.
--
-- A single tx can have multiple Exponent ix invocations (e.g. WrapperBuyYt
-- wraps BuyYt; some compound txs do strip+trade in one). We pick the
-- "headline" action via priority — the one that best represents the user's
-- intent. Wrapper variants are deduped: WrapperBuyYt + BuyYt counts as buyYt.
--
-- Action vocab (user-facing):
--   buyYt, sellYt, buyPt, sellPt   — trades (AMM swap-bearing)
--   addLiq, removeLiq               — LP
--   strip, merge                    — SY↔PT+YT conversion (no counterparty)
--   redeemPt                        — PT redemption at/after maturity
--   claimYield                      — yield/emission claims
--   deposit, withdraw               — non-trade SY mint/burn
--   admin                           — protocol admin / position init
--   unknown                         — none of the above matched
--
-- INCREMENTAL: log_messages comes directly from raw_helius_tx.payload
-- (not stg_helius_tx, which no longer carries the JSON columns). On
-- incremental runs only newly-fetched tx (payload still present) get
-- classified; old rows preserved.
{{ config(
    materialized='incremental',
    unique_key='signature',
    incremental_strategy='delete+insert'
) }}

with raw_logs as (
    select
        signature,
        block_time,
        slot,
        payload->'$.meta.logMessages' as log_messages
    from {{ source('raw', 'raw_helius_tx') }}
    where payload is not null
    {% if is_incremental() %}
      and block_time >= coalesce(
            (select max(block_time) from {{ this }}) - 86400,
            0
          )
    {% endif %}
),
raw_logs_with_messages as (
    select * from raw_logs where log_messages is not null
),
ix_names as (
    select
        signature,
        block_time,
        slot,
        list_distinct(
            list_filter(
                list_transform(
                    cast(log_messages as varchar[]),
                    line -> regexp_extract(line, 'Instruction:\s*(\w+)', 1)
                ),
                name -> name is not null and name <> ''
            )
        ) as ix_names
    from raw_logs_with_messages
)
select
    signature,
    block_time,
    slot,
    ix_names,
    case
        when list_contains(ix_names, 'BuyYt')           then 'buyYt'
        when list_contains(ix_names, 'SellYt')          then 'sellYt'
        when list_contains(ix_names, 'TradePt')         then 'tradePt'
        when list_contains(ix_names, 'MarketDepositLp') then 'addLiq'
        when list_contains(ix_names, 'MarketWithdrawLp')then 'removeLiq'
        when list_contains(ix_names, 'InitLpPosition')  then 'addLiq'
        when list_contains(ix_names, 'DepositYt')       then 'addLiq'
        when list_contains(ix_names, 'WithdrawYt')      then 'removeLiq'
        when list_contains(ix_names, 'Strip')           then 'strip'
        when list_contains(ix_names, 'Merge')           then 'merge'
        when list_contains(ix_names, 'Redeem')          then 'redeemPt'
        when list_contains(ix_names, 'StageYtYield')    then 'claimYield'
        when list_contains(ix_names, 'CollectInterest') then 'claimYield'
        when list_contains(ix_names, 'Deposit')         then 'deposit'
        when list_contains(ix_names, 'Withdraw')        then 'withdraw'
        when list_contains(ix_names, 'InitializeYieldPosition')   then 'admin'
        when list_contains(ix_names, 'InitializeMarket')          then 'admin'
        when list_contains(ix_names, 'InitializeVault')           then 'admin'
        when list_contains(ix_names, 'UpdatePrice')               then 'admin'
        when list_contains(ix_names, 'UpdateStakePoolBalance')    then 'admin'
        else 'unknown'
    end as action
from ix_names
