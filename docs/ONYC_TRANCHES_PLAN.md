# ONyc Tranches — Indexing & Transformation Plan

Parked plan for adding senior/junior tranche tracking to the dashboard.
Live since 2026-06-17. ONyc is the first (and currently only) tranche on Exponent.

## Background

Exponent launched a new product line: **risk tranches**. Separate Anchor program
(`XPTrnchoawiUc9iYJrpfchS8vgr8Y5X2QGBdHPXukty`), separate vault, separate LP tokens.

Not a YT/PT market — orthogonal mechanism. Same wONyc SY underlies it.

| Component | Pubkey |
|---|---|
| Tranche program | `XPTrnchoawiUc9iYJrpfchS8vgr8Y5X2QGBdHPXukty` |
| ONyc tranche vault | `HM8iLNE2WEN6J1AuwSSCoM37wgeLQFwYZ4ymYLqAoapN` |
| srONyc LP mint | `9J8VvigcjFTkN3jhZH2ieTi2hdGVBVpEXbcA1JDo7QpA` |
| jrONyc LP mint | `71j6BZaUPaSG1f1Y3e12KvU66d23kHgzWXhMDHn23wyB` |
| wONyc SY (shared with PT/YT) | `G1qbuP11CdquJCzuDjruWqatQAHroajmxhLfeQVgHosF` |
| ONyc underlying | `5Y8NV33Vv7WbnLfq3zBcKSdYPrk7g2KoiQoe7M2tcxp5` |
| Token escrow | `HZ7iDTzNcRKNfMTioG9NbxExtUuScrocHbSMrj7WfVdt` |

API endpoint: `https://api.exponent.finance/tranching-markets` (NOT `/markets`)

Docs: https://docs.exponent.finance/user-documentation/tranching-markets.md

## Available stats (from API; no on-chain decoding needed day 1)

### Coverage + TVL
- `coverageRatio` — jr/(jr+sr×mult); writedown if < `minCoverageRatio`
- `minCoverageRatio` — protection floor (currently 0.2)
- `marketSize` / `effectiveMarketSize` — total NAV
- `utilizationPct`
- `marketState` — lifecycle flag (1 = active)

### Senior
- `seniorApy`
- `srRawNetAssetValue` / `srEffectiveNetAssetValue`
- `srLpPriceNetAsset` / `srSyncedLpPriceNetAsset`
- `srMaxCapacityNetAssetValue` / `srRemainingCapacityNetAssetValue`
- `seniorPointsBoost.multiplier`

### Junior
- `juniorApy`
- `jrRawNetAssetValue` / `jrEffectiveNetAssetValue`
- `jrLpPriceNetAsset` / `jrSyncedLpPriceNetAsset`
- `jrMaxCapacityNetAssetValue` / `jrRemainingCapacityNetAssetValue`
- `juniorPointsBoosts[].multiplier`

### Underlying yield
- `underlyingApy` / `underlyingApy7d` / `underlyingApy30d`
- `currentSyExchangeRate` (cross-references vault `66R3TcKj…` we already track)

## Phase 1 — Indexing (~80 LOC, 1h)

**New extractor:** `extract_load/extract_tranche_states.py`
- GET `/tranching-markets`
- For each market: INSERT into `raw_tranche_states`
- Also `getAccountInfo(vault)` to capture slot + verify vault hasn't migrated

**New table** in [extract_load/load.py](../extract_load/load.py):

```sql
CREATE TABLE IF NOT EXISTS raw_tranche_states (
    snapshot_date          DATE NOT NULL,
    tranche_vault          VARCHAR NOT NULL,
    underlying_ticker      VARCHAR,
    coverage_ratio         DOUBLE,
    min_coverage_ratio     DOUBLE,
    market_size_usd        DOUBLE,
    effective_market_size  DOUBLE,
    utilization_pct        DOUBLE,
    market_state           INTEGER,
    senior_apy             DOUBLE,
    sr_nav_usd             DOUBLE,
    sr_effective_nav_usd   DOUBLE,
    sr_lp_price            DOUBLE,
    sr_max_capacity_usd    DOUBLE,
    sr_remaining_usd       DOUBLE,
    sr_points_mult         DOUBLE,
    junior_apy             DOUBLE,
    jr_nav_usd             DOUBLE,
    jr_effective_nav_usd   DOUBLE,
    jr_lp_price            DOUBLE,
    jr_max_capacity_usd    DOUBLE,
    jr_remaining_usd       DOUBLE,
    jr_points_mult         DOUBLE,
    underlying_apy         DOUBLE,
    underlying_apy_7d      DOUBLE,
    underlying_apy_30d     DOUBLE,
    sy_exchange_rate       DOUBLE,
    sr_lp_mint             VARCHAR,
    jr_lp_mint             VARCHAR,
    sy_mint                VARCHAR,
    base_mint              VARCHAR,
    start_ts               BIGINT,
    fetched_at             TIMESTAMP DEFAULT now(),
    PRIMARY KEY (snapshot_date, tranche_vault)
)
```

Also extend [extract_holders.py](../extract_load/extract_holders.py) to pull holders
for the 2 new LP mints (one-line config — same `getProgramAccounts` path as PT/YT/LP).

Wire into [ops/refresh_local.sh](../ops/refresh_local.sh) between `extract_market_three`
and `extract_pool_state`.

## Phase 2 — Transformation (~120 LOC, 1h)

**`transform/models/intermediate/int_tranche_coverage_daily.sql`** — per-day timeseries:
- coverage_ratio, jr_apy, sr_apy, jr_nav, sr_nav
- day-over-day coverage delta (how fast drifting toward floor)
- jr_capacity_pct = jrRemaining / jrMax
- sr_capacity_pct = srRemaining / srMax

**`transform/models/marts/analytics/tranche_summary.sql`** — latest + 7d/30d aggregates:
- current snapshot
- 7d / 30d min coverage ratio (worst case)
- 7d / 30d avg APY both tranches
- TVL trajectory
- one row per tranche vault (scales when they add more)

## Phase 3 — Serve + UI (~60 LOC, 30 min)

**`web/public/tranche.json`** (new payload):
```json
{
  "meta": { "generatedAt": "...", "tranches": ["onyc"] },
  "byVault": {
    "HM8iLNE2WEN6J1AuwSSCoM37wgeLQFwYZ4ymYLqAoapN": {
      "ticker": "ONyc",
      "current": { "coverage": 0.99, "jrApy": 0.118, "srApy": 0.081, "...": "..." },
      "dates": ["2026-06-17", "..."],
      "coverageRatio": [0.99, "..."],
      "juniorApy": ["..."],
      "seniorApy": ["..."],
      "jrNav": ["..."],
      "srNav": ["..."]
    }
  }
}
```

**UI:** add a "Tranches" tab on home page (parallel to Markets/Users). Single
section per vault with:
- Hero number: coverage ratio with 7d delta
- jr / sr APY side-by-side
- TVL split chart (jr vs sr stacked)
- Capacity meters (jr % full, sr % full)

## Phase 4 — Wallet integration (optional, ~30 LOC)

If a wallet holds srONyc or jrONyc, render a position line on
[web/app/wallet/page.tsx](../web/app/wallet/page.tsx). Same code path as existing
PT/YT/LP rows, just a new `leg` value.

## Total effort + risk

- **~250 LOC**, ~3h work
- **Risk: low** — additive snapshot extractor + view models, no migration risk
- **Schema migration**: one new table, uses existing `CREATE TABLE IF NOT EXISTS`
  pattern in [load.py:438](../extract_load/load.py#L438)
- **Railway cron impact**: +1 API call per refresh (negligible), +1 dbt model
  build (~1 sec)

## Deferred to a later iteration

- **Decoding the 1439-byte vault account** — would expose coverage-curve params
  (piecewise-linear slope / kink points) for reproducing the math ourselves.
  Useful if Exponent's API breaks; not critical day 1.
- **Settlement-cycle event extraction** — when 7-day cycles roll over, there's
  likely a `RebalanceTranche` / `SettleCycle` instruction. Indexing those gives
  per-cycle realized APY (vs the API's smoothed value). Worth adding once 2-3
  cycles exist to analyze.

## State at parking time (2026-06-25)

Snapshot from `/tranching-markets`:
- coverage_ratio: 0.9887
- min_coverage_ratio: 0.2
- market_size: $245,332
- utilization: 20.23%
- senior_apy: 8.11%  (NAV $2,773, capacity $970k, points 1×)
- junior_apy: 11.82% (NAV $242,558, capacity $449k, points 6×)
- underlying_apy: 11.78% (7d & 30d same — fresh vault)
- sy_exchange_rate: 1.1172
- start: 2026-06-17 14:08 UTC (8 days ago)
