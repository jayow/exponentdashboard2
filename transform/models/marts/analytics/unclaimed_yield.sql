-- Per-wallet markets with outstanding YT but no claimYield ever fired.
--
-- For each (wallet, market) where the wallet currently holds YT and has
-- never invoked claimYield (StageYtYield / CollectInterest) on that market,
-- flag the market as having unclaimed yield.
--
-- We can't compute USD-value of unclaimed yield without per-second yield
-- accrual rates (would need implied APY × YT-days held). For now this
-- mart surfaces *which markets* have unclaimed positions — the v1
-- "unclaimed yield" warning chips reproduce from this.
{{ config(materialized='view') }}

with yt_holdings as (
    -- Wallets currently holding YT (latest snapshot, balance > 0)
    select
        ml.market_key,
        h.owner            as wallet,
        h.amount           as yt_balance
    from {{ source('raw', 'raw_holders') }} h
    join (
        select pt_mint as mint, market_key, 'PT' leg from {{ ref('dim_markets') }} where pt_mint is not null
        union all select yt_mint, market_key, 'YT' from {{ ref('dim_markets') }} where yt_mint is not null
    ) ml on ml.mint = h.mint
    where ml.leg = 'YT' and h.amount > 0.000001
      and h.snapshot_date = (
        select max(snapshot_date) from {{ source('raw', 'raw_holders') }} h2 where h2.mint = h.mint
      )
),
claims as (
    -- Wallets that have ever fired a claimYield for a given market
    select distinct e.signer as wallet, e.market_key
    from {{ ref('wallet_events') }} e
    where e.action = 'claimYield' and e.market_key is not null
)
select
    yt.wallet,
    yt.market_key,
    yt.yt_balance
from yt_holdings yt
left join claims c on c.wallet = yt.wallet and c.market_key = yt.market_key
where c.wallet is null
