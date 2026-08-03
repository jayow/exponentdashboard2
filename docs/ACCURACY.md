# Dashboard Accuracy Framework

How we verify every number the dashboard publishes is correct — systematically,
not ad hoc. Born from the TVL audit (SY supply was 31% off) and the volume audit
(verified exact): the two had *different* failure modes, which is the whole point.

## Core principle

Every metric traces to a **primitive**. A primitive is captured in exactly one of
five ways, and **each way fails differently** — so each needs different checks.
Accuracy = the primitive is (a) captured completely, (b) captured correctly, and
(c) computed correctly downstream.

| # | Primitive type | Examples | Failure mode | Drift risk |
|---|---|---|---|---|
| P1 | **Authoritative state** (read account directly) | mint supply (`raw_mint_supplies`←SPL Mint), holders (`raw_holders`←token accts), Vault/StrategyVault/MarketTwo/pool/v2-book decodes | incomplete coverage (gPA 429 → missing accounts) or decode error (wrong offsets) | low |
| P2 | **Per-transaction flow** | volume (`fct_swaps`), claims, LP events, wallet events | missed txs (signature-discovery gap) or mis-attribution | low |
| P3 | **Reconstructed** (cumulative tx-delta) | `int_mint_supplies_daily` (PT/YT supply) | **DRIFT** — any missed delta accumulates forever | **HIGH** |
| P4 | **External** | prices (Jupiter/Pyth), Exponent registry/metadata | staleness, source error, missing | medium |
| P5 | **Derived** (computed) | TVL, implied yield, decompositions, market share, user & strategy metrics | computation error + inherited input errors | inherits |

The SY-supply bug was a P3 (reconstruction drifted 31%). Volume is P2 (per-tx,
verified 10/10 against chain). Same dashboard, opposite risk — because different
primitive.

## The check catalog (the "how")

Fifteen checks, grouped by which primitive they defend. IDs are referenced in the
matrix below and implemented in `ops/accuracy_check.py`.

**For P1 — authoritative state**
- **C1 Coverage** — count of captured accounts == on-chain count (`getProgramAccounts`/`getTokenSupply`). Catches gPA gaps.
- **C2 Per-item reconciliation** — sample N, re-decode from the raw account, must match. Catches decode/decimal errors.
- **C3 Decode cross-validation** — a decoded field equals an independent on-chain source (e.g. `Vault.pt_supply` == PT mint supply). Catches offset drift.

**For P2 — per-tx flow**
- **C4 Continuity** — daily count/volume has no cliff or gap; a sudden drop signals missed txs.
- **C5 Per-tx spot-check** — sample N, re-derive notional from the raw tx, must match (volume: 10/10 exact).
- **C6 Signature coverage** — our tx count for an address == `getSignaturesForAddress` count.

**For P3 — reconstruction (highest priority)**
- **C7 Endpoint-anchor** — cumulative value *today* == the authoritative value today (`getTokenSupply`, account decode). **>1% off = drift.** This single check would have caught the SY bug automatically.
- **C8 Authoritative-available flag** — flag any metric still sourced from a reconstruction when an authoritative primitive exists (it should migrate).

**For P4 — external**
- **C9 Freshness** — `max(price date)` == today; no forward-fill older than N days.
- **C10 Sanity bounds** — stablecoins ∈ [0.90, 1.10]; no zero/negative; no absurd (>1e6).
- **C11 Cross-source** — Jupiter vs Pyth agree within tolerance where both exist.
- **C12 Price completeness** — every SY/underlying mint that carries supply has a price (unpriced → silently dropped from TVL).

**For P5 — derived**
- **C13 Invariants** — decomposition sums to total (income+farm+lp+idle == TVL); market shares == 100%; PT supply ≡ YT supply; buy+sell == total volume.
- **C14 External reference** — implied yield vs Exponent `impliedApy`; our PT-principal vs Exponent `totalMarketSize`; TVL vs DefiLlama. **Sanity references, never fit targets** — on-chain wins.
- **C15 Range/known-issue** — no negative TVL/volume; APY, LTV, leverage bounded; loop obligations have debt≠0; `dim_markets.status` consistent with maturity; tranche `underlyingApy7d/30d` not literal copies.

## Metric × check matrix (the entirety)

Every published surface, its primitive, its checks, and current risk.

| Output | Metric | Primitive | Checks | Risk |
|---|---|---|---|---|
| `tvl.json` | protocol / per-market TVL | P1 SY supply × P4 price | C1, C7, C12, C13, C14 | **fixed** (was HIGH P3) |
| `tvl.json` | decomposition income/farm/lp/idle | P3 PT supply + P1 SY | **C7 (PT anchor)**, C13 (sum==TVL) | MED — PT still P3 (0.2% now) |
| `tvl.json` | `impliedYieldSeries` | P5 `int_market_iy_daily` (curve) | C9 (curve fresh), C14 (vs Exponent) | MED |
| `tvl.json` | TVL by platform | P5 aggregation | C13 (sums to protocol) | LOW |
| `volume.json` | total / per-market / buy-sell | P2 `fct_swaps` | C4, C5, C6, C13 | LOW — verified |
| `stats.json` | protocol headline stats | P5 aggregation | C13 (cross-JSON consistency) | LOW |
| `users.json` | user growth / counts | P5 from `dim_users` (P2 swaps) | C4, C13 | LOW-MED |
| `market_holders.json` | top holders + entry IY | P1 `raw_holders` (gPA) + P5 `holder_entry_iy` | C1, C2, C15 (entryIY sane) | MED — gPA coverage |
| `holders.json` | holder counts | P1 `raw_holders` | C1, C2 | MED |
| `market_share.json` | shares | P5 aggregation | C13 (==100%) | LOW |
| `active_positions.json` | positions | P5 derived | C13, C15 | LOW |
| `unclaimed_yield.json` | unclaimed yield | P1 positions/vault state | C2, C15 (bounds) | MED |
| `v2_orderbook.json` | bid/ask/depth | P1 XPBook decode | C2 (decode), C15 (bid<ask) | MED |
| `tranche.json` | senior/junior APY | P5 tranche models | C9 (null-prop), **C15 (known bugs)** | **HIGH — known issues** |
| `strategy_vault.json` | NAV / AUM | P1 StrategyVault decode | C2, C3 | MED |
| `strategy_vault.json` | Kamino loop LTV/leverage/debt | P1 obligation decode | C2, **C15 (debt≠0)** | **HIGH — debt=0 seen** |
| `strategy_vault.json` | withdrawal queue | P1 account | C2 | MED |
| `strategy_vault.json` | flows | P2 tx-derived | C4, C5 | MED |
| `strategy_vault.json` | holders | P1 gPA | C1, C2 | MED |
| `strategy_vault.json` | governance proposals | P1 decode | C2, C3 | MED |
| `strategy_vault.json` | APY | P4 registry | C14 (vs Exponent) | MED |
| `wallet/*.json` | per-wallet positions/events | P5 `wallet_summary` | C2 (vs holdings), C4 | MED |
| `strategy-txns/*.json` | vault txns | P2 `wallet_events` | C4, C6 | MED |
| `dim_markets` | maturity / status | P4 metadata | C15 (status vs maturity — known bug) | LOW |

## Priority & cadence

- **Every refresh (fast, in-warehouse — no RPC):** C4, C7, C10, C12, C13, C15.
  These are cheap SQL and catch the dangerous classes (drift, broken invariants,
  stale/absent prices, impossible values). **C7 is the single highest-value check**
  — it turns the SY-class of silent drift into an automatic red flag.
- **Daily deep (RPC):** C1, C2, C5, C6, C9, C11, C14. On-chain reconciliation and
  cross-source; slower, run once/day or on demand.
- **On new metric:** classify its primitive (P1–P5) and wire the matching checks
  before shipping. A P3 (reconstruction) must ship with a C7 anchor.

## Supply: where the number comes from, and how to verify it

**`getTokenSupply` is the oracle, not a data source.** The indexer's job is to
derive supply from on-chain events and *agree* with it. If it disagrees, the
indexer is wrong — reaching for the direct read as the answer patches the
symptom and leaves history broken. That distinction is the whole lesson of the
2026-08-02 incident below.

Precedence actually applied (`int_mint_supplies_daily` → `int_sy_tvl_daily`):

1. **`raw_mint_supplies`** — on-chain SPL Mint supply (`extract_mint_supplies`).
   Authoritative, but point-in-time: only exists for dates we snapshotted, and
   there is no historical form of the call. Treat as a **safety net**, not the
   thing holding the numbers up.
2. **`raw_sy_supply_history`** — derivable-correct daily SY supply
   (`extract_sy_supply_history`). Anchors on today's authoritative read and
   walks *backward* through real mint/burn txs. This is how you legitimately
   backfill: never copy a current value across past dates — supply genuinely
   moves (USX SY was 54.7M on 07-11 and 52.7M on 08-02), so flat-lining it
   invents movement that looks plausible.
3. **`int_mint_supplies_daily`** — cumulative tx-delta reconstruction. P3, and
   only as good as signature coverage. **This is the one that should be right**;
   1 and 2 exist because it wasn't.

Verification ladder, weakest to strongest:

- **Internal invariants** (C13b etc.) — cheap, catches impossible states, but
  passing proves only self-consistency.
- **`getTokenSupply`** — direct chain read. Settles whether a supply is right.
- **`api.exponent.finance/markets`** — `totalMarketSize` is PT supply in
  underlying units; `syExchangeRate` is the wrapper's accrual. Best per-market
  cross-check; 12/12 markets matched on 2026-08-02, 11 to the cent.
- **DefiLlama** — same formula as ours (supply × rate × underlying price) per
  their adapter. Useful, but a mismatch is not automatically ours: on
  2026-08-02 we sat ~8% under theirs while matching Exponent itself exactly.

Do **not** apply `syExchangeRate` on top of our price. Our feed prices the
*wrapper*, so accrual is already in it; applying the rate again double-counts.
Verified per-market: our implied price ÷ Exponent's rate lands on the
underlying asset price (ONyc 0.9988, eUSX 0.9980, USX 0.9993).

## Incident 2026-08-02: SY supply 33% low (root cause: watch-address gap)

Worth reading before touching supply, because every instinct here was wrong at
least once.

**Symptom.** C13b FAIL — principal $94.1M > SY TVL $89.9M, an impossible state
(PT is a claim on SY, so SY ⊇ PT). Downstream, Solstice's headline TVL read
$33.5M against a true $59.8M, manufacturing a three-week "decline" and an
OnRe-overtakes-Solstice crossover **that never happened**.

**Root cause.** `extract_signatures` watched vault / ammPool / clmmOrderbook /
pool / ptMint / ytMint — but **not `syMint`**. Wrapping underlying → SY touches
the SY program and the SY mint but need not touch any watched address, so those
txs never entered `raw_signatures`. Measured: **127 of the last 1000 USX SY-mint
txs missing (12.7%)**; C6 later found 24.3% on another mint. Since
`int_mint_supplies_daily` sums tx deltas, missing mint/burn legs made SY
reconstruct 33% low — while PT, whose mint *was* watched, stayed accurate to
0.04%. That asymmetry is the tell, and it is exactly why principal exceeded SY.

**Why the obvious fixes don't work.**
- *Copy today's supply backward* — fabricates movement (see above).
- *Anchor at the last good snapshot and add reconstruction deltas* — the deltas
  are wrong too, not just the level: reconstruction showed −9.4M over the
  window against −2.0M actual.
- *Overlay authoritative snapshots* — what was done first. Makes today's number
  read correctly while the indexer stays broken, so history stays broken. Keep
  it, but as a safety net.

**Fixes applied.** `syMint` added to the watch list (root cause);
`int_mint_supplies_daily` overlays authoritative for PT/YT as well as SY;
`extract_sy_supply_history` re-run to rebuild the drifted window; **C6
implemented** (see below); Makefile brought to extractor parity with
`refresh.yml` — it was missing `extract_mint_supplies`, so `make refresh`
silently produced no authoritative snapshot at all and the 21-day-stale one let
the drift through unseen.

**C6 was the check that would have caught this on day one.** It was in the
catalog and the harness docstring claimed `--deep` ran it, but it had never been
implemented — only C1 and C5 had. Now implemented. A coverage gap is upstream of
every P3 reconstruction: C7 tells you the total drifted, C6 tells you *why*.
**Run `--deep` when supply looks off; C7 alone will not name the cause.**

## Harness

`ops/accuracy_check.py`:
- default: runs all in-warehouse checks, prints a PASS/FAIL table, exits non-zero on any FAIL.
- `--deep`: additionally runs the RPC on-chain reconciliation checks. **Implemented:
  C1, C5, C6.** C2 is still catalog-only — do not assume a catalog entry is wired
  up; that assumption is what let the SY bug run unseen (see incident above).
- `--json`: machine-readable output for wiring into the refresh pipeline's validate step.

Note the default run is deliberately RPC-free so it can gate every refresh, but
that means **it cannot detect a coverage gap** — C6 needs the chain. Nothing in
the daily pipeline will catch a new watch-address hole; run `--deep` after
adding a market type, changing `watch_addresses()`, or whenever a P3 number
looks off.
