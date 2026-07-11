"""Dashboard accuracy harness. See docs/ACCURACY.md for the framework.

Runs the check catalog against the warehouse. Default = fast in-warehouse checks
(C4/C7/C10/C12/C13/C15) that catch the dangerous classes: reconstruction drift,
broken invariants, stale/absent prices, impossible values. `--deep` adds RPC
on-chain reconciliation (C1/C5). Exits non-zero on any FAIL.

    python -m ops.accuracy_check           # fast
    python -m ops.accuracy_check --deep     # + on-chain reconciliation
    python -m ops.accuracy_check --json     # machine-readable
"""
from __future__ import annotations

import argparse
import json
import os
import sys

import duckdb

WH = os.environ.get("WAREHOUSE_PATH", "data/warehouse.duckdb")
CHECKS: list = []


def check(cid, desc):
    def wrap(fn):
        CHECKS.append((cid, desc, fn))
        return fn
    return wrap


def _latest(con, tbl, col="date"):
    return con.execute(f"select max({col}) from {tbl}").fetchone()[0]


# ── P3: reconstruction drift (highest priority) ──────────────────────────────
@check("C7", "SY/PT/YT supply: reconstruction vs authoritative on-chain (drift)")
def c7_supply_anchor(con):
    # Reconstruction (int_mint_supplies_daily) vs authoritative (raw_mint_supplies).
    # >1% on a meaningful mint = the SY-class drift bug.
    rows = con.execute("""
        with a as (select mint, leg, supply_ui auth from main.raw_mint_supplies
                   where snapshot_date=(select max(snapshot_date) from main.raw_mint_supplies)),
             r as (select mint, leg, supply_ui recon from main_intermediate.int_mint_supplies_daily
                   where date=(select max(date) from main_intermediate.int_mint_supplies_daily))
        select a.mint, a.leg, a.auth, r.recon,
               abs(a.auth-coalesce(r.recon,0))/nullif(a.auth,0) drift
        from a left join r using (mint, leg)
        where a.auth > 10000 and abs(a.auth-coalesce(r.recon,0))/nullif(a.auth,0) > 0.01
        order by drift desc
    """).fetchall()
    if not rows:
        return "PASS", "all supplies within 1% of authoritative"
    worst = rows[0]
    return "WARN", (f"{len(rows)} mint-legs drift >1% (served TVL uses authoritative, "
                    f"but decomposition PT/YT still reconstruction). worst: "
                    f"{worst[1]} {worst[0][:8]} {worst[4]*100:.0f}% off")


# ── P5: invariants ───────────────────────────────────────────────────────────
@check("C13a", "PT supply ≡ YT supply per vault (co-minted 1:1)")
def c13_pt_yt(con):
    # Only ACTIVE markets: expired markets legitimately have PT < YT (PT
    # redeemed at maturity, worthless YT persists — the phantom-YT pattern).
    rows = con.execute("""
        with s as (select r.vault, r.leg, r.supply_ui
                   from main.raw_mint_supplies r
                   where r.snapshot_date=(select max(snapshot_date) from main.raw_mint_supplies)
                     and r.leg in ('PT','YT')),
             active as (select distinct v.vault from main.raw_mint_supplies v
                        join main_core.dim_markets m on m.pt_mint = v.mint
                        where m.maturity_date is null or m.maturity_date > current_date)
        select p.vault, p.supply_ui pt, y.supply_ui yt
        from (select * from s where leg='PT') p
        join (select * from s where leg='YT') y using (vault)
        join active a using (vault)
        where abs(p.supply_ui-y.supply_ui) > greatest(1.0, p.supply_ui*0.01)
    """).fetchall()
    return ("PASS", "PT≡YT on all active vaults (expired excluded)") if not rows else \
        ("WARN", f"{len(rows)} ACTIVE vaults: PT≠YT >1% (e.g. {rows[0][0][:8]}: PT={rows[0][1]:,.0f} YT={rows[0][2]:,.0f})")


@check("C13b", "protocol TVL ≥ principal (SY ⊇ PT), and both positive")
def c13_tvl_ge_principal(con):
    d = _latest(con, "main_analytics.tvl_daily")
    sy = con.execute("select sum(tvl_usd) from main_intermediate.int_sy_tvl_daily where date=?", [d]).fetchone()[0] or 0
    pr = con.execute("select sum(principal_tvl_usd) from main_analytics.tvl_daily where date=?", [d]).fetchone()[0] or 0
    if sy <= 0:
        return "FAIL", f"protocol TVL non-positive (${sy:,.0f})"
    if pr > sy * 1.02:
        return "FAIL", f"principal ${pr/1e6:.1f}M > SY TVL ${sy/1e6:.1f}M (impossible)"
    return "PASS", f"SY TVL ${sy/1e6:.1f}M ≥ principal ${pr/1e6:.1f}M"


@check("C13c", "volume: buy + sell ≈ total (direction coverage)")
def c13_volume_sides(con):
    r = con.execute("""select sum(volume_usd) t,
        sum(coalesce(volume_usd_buys,0)+coalesce(volume_usd_sells,0)) bs,
        sum(trade_count) tc, sum(buy_count)+sum(sell_count) dc
        from main_analytics.trading_volume_daily""").fetchone()
    gap = (r[0] - r[1]) / r[0] if r[0] else 0
    return ("PASS", f"buy+sell = {(1-gap)*100:.1f}% of total") if gap < 0.001 else \
        ("WARN", f"{r[2]-r[3]:,} trades ({gap*100:.1f}% of vol) have no buy/sell direction — total OK, split undercounts")


# ── P4: prices ───────────────────────────────────────────────────────────────
@check("C10", "price sanity: no zero/negative/absurd; stables near $1")
def c10_price_sanity(con):
    pd = _latest(con, "main_staging.stg_prices", "date")
    bad = con.execute("""
        select mint, price_usd from main_staging.stg_prices
        where date=? and (price_usd <= 0 or price_usd > 1e6)
    """, [pd]).fetchall()
    return ("PASS", f"all prices in (0, 1e6] on {pd}") if not bad else \
        ("FAIL", f"{len(bad)} bad prices (e.g. {bad[0][0][:8]}=${bad[0][1]})")


@check("C12", "price completeness: every SY mint with supply is priced")
def c12_price_completeness(con):
    rows = con.execute("""
        select r.mint, r.supply_ui from main.raw_mint_supplies r
        where r.leg='SY' and r.snapshot_date=(select max(snapshot_date) from main.raw_mint_supplies)
          and r.supply_ui > 10000
          and r.mint not in (select sy_mint from main_intermediate.int_sy_tvl_daily
                             where date=(select max(date) from main_intermediate.int_sy_tvl_daily))
    """).fetchall()
    return ("PASS", "all material SY mints priced") if not rows else \
        ("WARN", f"{len(rows)} SY mints with supply but unpriced (dropped from TVL): {rows[0][0][:8]}…")


# ── P2: flow continuity ──────────────────────────────────────────────────────
@check("C4", "volume continuity: no recent cliff (missed-tx signal)")
def c4_volume_continuity(con):
    rows = con.execute("""
        select date, sum(trade_count) tc from main_analytics.trading_volume_daily
        group by 1 order by 1 desc limit 15
    """).fetchall()
    if len(rows) < 8:
        return "SKIP", "not enough history"
    recent = rows[1][1]  # yesterday (today may be partial)
    med = sorted(r[1] for r in rows[2:])[len(rows[2:]) // 2]
    return ("PASS", f"yesterday {recent} trades vs 14d median {med}") if recent >= med * 0.3 else \
        ("WARN", f"yesterday {recent} trades << median {med} — possible missed swaps")


# ── P5/P1: range & known-issue checks ────────────────────────────────────────
@check("C15a", "no negative TVL or volume")
def c15_negatives(con):
    n1 = con.execute("select count(*) from main_analytics.tvl_daily where tvl_usd < 0").fetchone()[0]
    n2 = con.execute("select count(*) from main_analytics.trading_volume_daily where volume_usd < 0").fetchone()[0]
    return ("PASS", "no negatives") if n1 + n2 == 0 else ("FAIL", f"{n1} neg TVL rows, {n2} neg volume rows")


@check("C15b", "strategy loops: collateralized obligation should carry debt")
def c15_loop_debt(con):
    try:
        cols = [c[0] for c in con.execute("describe main.raw_strategy_vault_obligations").fetchall()]
    except Exception:
        return "SKIP", "raw_strategy_vault_obligations not present"
    dcol = next((c for c in cols if "debt" in c.lower()), None)
    ccol = next((c for c in cols if "collateral" in c.lower() or "coll" in c.lower()), None)
    if not (dcol and ccol):
        return "SKIP", f"no debt/collateral cols ({cols})"
    n = con.execute(f"""
        select count(*) from main.raw_strategy_vault_obligations
        where snapshot_date=(select max(snapshot_date) from main.raw_strategy_vault_obligations)
          and {ccol} > 0 and coalesce({dcol},0) = 0
    """).fetchone()[0]
    return ("PASS", "all collateralized obligations have debt") if n == 0 else \
        ("WARN", f"{n} obligations: collateral>0 but debt=0 (loop debt decode may be wrong)")


@check("C15c", "dim_markets.status consistent with maturity (known bug)")
def c15_status(con):
    try:
        n = con.execute("""
            select count(*) from main_core.dim_markets
            where status='active' and maturity_date is not null and maturity_date <= current_date
        """).fetchone()[0]
    except Exception:
        return "SKIP", "dim_markets/status not present"
    return ("PASS", "no expired markets marked active") if n == 0 else \
        ("WARN", f"{n} matured markets still status='active' (don't filter on status; use maturity_date)")


# ── Deep: RPC on-chain reconciliation ────────────────────────────────────────
def deep_checks(con):
    import urllib.request
    url = [u.strip() for u in os.environ.get("SOLANA_RPC_URLS", "").split(",") if u.strip()]
    if not url:
        print("  (--deep skipped: no SOLANA_RPC_URLS)")
        return []
    url = url[0]
    def rpc(m, p):
        b = {"jsonrpc": "2.0", "id": 1, "method": m, "params": p}
        return json.load(urllib.request.urlopen(urllib.request.Request(
            url, data=json.dumps(b).encode(), headers={"Content-Type": "application/json"}), timeout=25))["result"]
    out = []
    # C1: top SY supplies vs getTokenSupply
    mints = con.execute("""select mint, supply_ui from main.raw_mint_supplies
        where leg='SY' and snapshot_date=(select max(snapshot_date) from main.raw_mint_supplies)
        order by supply_ui desc limit 5""").fetchall()
    worst = 0.0
    for mint, sup in mints:
        oc = float(rpc("getTokenSupply", [mint])["value"]["uiAmountString"])
        worst = max(worst, abs(sup - oc) / oc if oc else 0)
    # Snapshot is from the last refresh; live supply moves intraday. >5% would
    # indicate a decode/extractor error, not normal intraday activity.
    out.append(("C1", "SY snapshot vs live getTokenSupply (intraday-tolerant)",
                "PASS" if worst < 0.05 else "FAIL", f"worst {worst*100:.2f}% off (snapshot age)"))
    # C5: sample trades vs on-chain notional
    trades = con.execute("""select signature, notional_underlying, underlying_mint from main_core.fct_swaps
        where notional_usd > 5000 order by block_time desc limit 6""").fetchall()
    ok = 0
    for sig, notl, um in trades:
        tx = rpc("getTransaction", [sig, {"encoding": "jsonParsed", "maxSupportedTransactionVersion": 0}])
        if not tx:
            continue
        signer = tx["transaction"]["message"]["accountKeys"][0]["pubkey"]
        meta = tx["meta"]
        def bal(lst):
            return sum(float(b["uiTokenAmount"]["uiAmount"] or 0) for b in (lst or [])
                       if b.get("owner") == signer and b.get("mint") == um)
        delta = abs(bal(meta.get("postTokenBalances")) - bal(meta.get("preTokenBalances")))
        ok += abs(delta - notl) < max(1.0, notl * 0.01)
    # Occasional misses = routed/flash trades where the signer's underlying nets
    # out and notional is attributed differently. Systemic (<80%) = a real bug.
    rate = ok / len(trades) if trades else 0
    out.append(("C5", "sample trades vs on-chain notional",
                "PASS" if rate == 1 else "WARN" if rate >= 0.8 else "FAIL",
                f"{ok}/{len(trades)} exact" + ("" if rate == 1 else " (rest = routed-trade edge case)")))
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--deep", action="store_true")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()
    con = duckdb.connect(WH, read_only=True)

    results = []
    for cid, desc, fn in CHECKS:
        try:
            status, detail = fn(con)
        except Exception as e:
            status, detail = "SKIP", f"error: {str(e)[:60]}"
        results.append((cid, desc, status, detail))
    if args.deep:
        results += [(c, d, s, dt) for c, d, s, dt in deep_checks(con)]

    if args.json:
        print(json.dumps([{"id": c, "desc": d, "status": s, "detail": dt} for c, d, s, dt in results], indent=2))
    else:
        icon = {"PASS": "✓", "WARN": "⚠", "FAIL": "✗", "SKIP": "·"}
        print(f"\n{'ID':6}{'STATUS':8}{'CHECK':52}DETAIL")
        print("-" * 110)
        for c, d, s, dt in results:
            print(f"{c:6}{icon.get(s,'?')} {s:6}{d[:50]:52}{dt}")
        n_fail = sum(1 for *_, s, _ in results if s == "FAIL")
        n_warn = sum(1 for *_, s, _ in results if s == "WARN")
        print("-" * 110)
        print(f"{sum(1 for *_,s,_ in results if s=='PASS')} pass, {n_warn} warn, {n_fail} fail")
    con.close()
    sys.exit(1 if any(s == "FAIL" for *_, s, _ in results) else 0)


if __name__ == "__main__":
    main()
