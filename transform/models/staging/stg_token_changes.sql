-- Per-tx, per-(account_index, mint, owner) token balance deltas.
-- Computed from pre_token_balances and post_token_balances meta fields.
--
-- Some accounts appear only in pre (closed) or only in post (newly opened) —
-- a FULL OUTER JOIN with COALESCE handles both, treating missing as 0.
--
-- A `delta` of 0 means the account ended the tx with the same balance it
-- started with — filtered out, no useful signal.
--
-- Key fields:
--   account_index — position in the tx's accountKeys array (lets you map
--                   back to the actual address via stg_helius_tx.account_keys)
--   owner         — wallet/PDA that owns the token account
--   mint          — token mint
--   delta_raw     — post.amount - pre.amount (integer, raw decimals)
--   delta_ui      — same in display units (delta_raw / 10^decimals)
{{ config(materialized='table') }}

with pre as (
    select
        s.signature,
        s.block_time,
        cast(b.value->>'$.accountIndex' as integer)         as account_index,
        b.value->>'$.mint'                                   as mint,
        b.value->>'$.owner'                                  as owner,
        cast(b.value->'$.uiTokenAmount'->>'$.amount' as hugeint) as amount,
        cast(b.value->'$.uiTokenAmount'->>'$.decimals' as int)   as decimals
    from {{ ref('stg_helius_tx') }} s,
        unnest(cast(s.pre_token_balances as json[])) as b(value)
    where s.pre_token_balances is not null
),
post as (
    select
        s.signature,
        s.block_time,
        cast(b.value->>'$.accountIndex' as integer)         as account_index,
        b.value->>'$.mint'                                   as mint,
        b.value->>'$.owner'                                  as owner,
        cast(b.value->'$.uiTokenAmount'->>'$.amount' as hugeint) as amount,
        cast(b.value->'$.uiTokenAmount'->>'$.decimals' as int)   as decimals
    from {{ ref('stg_helius_tx') }} s,
        unnest(cast(s.post_token_balances as json[])) as b(value)
    where s.post_token_balances is not null
)
select
    coalesce(pre.signature, post.signature)             as signature,
    coalesce(pre.block_time, post.block_time)           as block_time,
    coalesce(pre.account_index, post.account_index)     as account_index,
    coalesce(pre.mint, post.mint)                       as mint,
    coalesce(pre.owner, post.owner)                     as owner,
    coalesce(post.decimals, pre.decimals)               as decimals,
    coalesce(post.amount, 0)
        - coalesce(pre.amount, 0)                       as delta_raw,
    (coalesce(post.amount, 0) - coalesce(pre.amount, 0))::double
        / pow(10, coalesce(post.decimals, pre.decimals, 0))::double  as delta_ui
from pre
full outer join post
    on  pre.signature     = post.signature
    and pre.account_index = post.account_index
    and pre.mint          = post.mint
    and pre.owner         = post.owner
where coalesce(post.amount, 0) <> coalesce(pre.amount, 0)
