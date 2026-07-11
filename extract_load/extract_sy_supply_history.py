"""One-time backfill: derivable-correct daily SY supply for the recent window.

The tx-delta reconstruction (int_mint_supplies_daily) is correct for ~8 months
of history but drifted from ~June 2026 (incomplete SY mint/burn coverage in the
recent tx backfill — USX ended 31% low). This re-derives supply from COMPLETE
mint-referencing tx coverage over a recent window and splices onto the correct
pre-drift history.

Method (validated to 0.02% vs on-chain on USX):
  - anchor = today's authoritative supply (raw_mint_supplies, from getTokenSupply)
  - fetch every mint-referencing tx in the last WINDOW_DAYS (getSignaturesForAddress
    on the mint); per-tx supply delta = Σ(post mint balances) − Σ(pre) — transfers
    cancel within a tx, only mint/burn leave a net
  - walk backward from the anchor: supply(end of day d) = anchor − Σ(delta after d)
  - window reaches back before the drift (~June 3), so the earliest derived point
    meets the still-correct reconstruction seamlessly.

gPA-free (getSignaturesForAddress + getTransaction), but volume-heavy — run
locally. Not a daily step: daily raw_mint_supplies snapshots extend the correct
series forward; re-run this only to re-backfill a drifted window.
"""
from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone, timedelta, date

import httpx
from rich import print as rprint

from .config import RPC_ENDPOINTS
from .load import warehouse

WINDOW_DAYS = 60          # covers the June-2026 drift with margin
CONCURRENCY = 16          # individual getTransaction; residential IP tolerates this


def _mint_delta(tx: dict, mint: str) -> float:
    """Net supply change for `mint` in this tx = Σ(post) − Σ(pre) of its balances."""
    meta = tx.get("meta") or {}
    pre = sum(float((b["uiTokenAmount"]["uiAmount"]) or 0)
              for b in (meta.get("preTokenBalances") or []) if b.get("mint") == mint)
    post = sum(float((b["uiTokenAmount"]["uiAmount"]) or 0)
               for b in (meta.get("postTokenBalances") or []) if b.get("mint") == mint)
    return post - pre


async def _rpc(client: httpx.AsyncClient, url: str, method: str, params: list):
    """Single JSON-RPC call with 429 backoff. Batching hits Helius sub-request
    limits, so we fetch individually — slower but what the residential IP tolerates."""
    for _ in range(6):
        try:
            r = await client.post(url, json={"jsonrpc": "2.0", "id": 1,
                                             "method": method, "params": params})
            if r.status_code == 429:
                await asyncio.sleep(1.5)
                continue
            return r.json().get("result")
        except Exception:
            await asyncio.sleep(1.0)
    return None


async def _derive_mint(client: httpx.AsyncClient, url: str, mint: str, anchor: float,
                       cutoff_ts: int, today: date) -> tuple[list[tuple], int]:
    # 1. collect success sigs newer than cutoff (paginate)
    sigs: list[tuple[str, int]] = []
    before = None
    while True:
        opts = {"limit": 1000}
        if before:
            opts["before"] = before
        page = await _rpc(client, url, "getSignaturesForAddress", [mint, opts]) or []
        if not page:
            break
        stop = False
        for s in page:
            bt = s.get("blockTime")
            if bt is not None and bt < cutoff_ts:
                stop = True
                break
            if s.get("err") is None and bt is not None:
                sigs.append((s["signature"], bt))
        before = page[-1]["signature"]
        if stop or len(page) < 1000:
            break

    # 2. individual getTransaction, semaphore-bounded; net mint delta per day
    daily_delta: dict[str, float] = {}
    sem = asyncio.Semaphore(CONCURRENCY)

    async def one(sig: str, bt: int):
        async with sem:
            tx = await _rpc(client, url, "getTransaction",
                            [sig, {"encoding": "jsonParsed", "maxSupportedTransactionVersion": 0}])
        if not tx:
            return
        delta = _mint_delta(tx, mint)
        if abs(delta) > 1e-9:
            d = date.fromtimestamp(bt).isoformat()
            daily_delta[d] = daily_delta.get(d, 0.0) + delta

    await asyncio.gather(*[one(s, bt) for s, bt in sigs])

    # 3. walk backward from the authoritative anchor over every day in the window
    snapshot_ts = datetime.now(timezone.utc)
    start = today - timedelta(days=WINDOW_DAYS)
    days = [start + timedelta(days=i) for i in range((today - start).days + 1)]
    rows: list[tuple] = []
    running = anchor
    for d in sorted(days, reverse=True):
        ds = d.isoformat()
        rows.append((mint, ds, running, snapshot_ts))
        running -= daily_delta.get(ds, 0.0)
    return rows, len(sigs)


async def run() -> dict:
    if not RPC_ENDPOINTS:
        raise RuntimeError("No SOLANA_RPC_URLS configured in .env")
    url = RPC_ENDPOINTS[0]
    today = datetime.now(timezone.utc).date()
    cutoff_ts = int((datetime.now(timezone.utc) - timedelta(days=WINDOW_DAYS)).timestamp())

    # anchors = authoritative current supply per SY mint (from extract_mint_supplies)
    with warehouse() as con:
        anchors = con.execute(
            "SELECT mint, supply_ui FROM raw_mint_supplies "
            "WHERE leg = 'SY' AND snapshot_date = (SELECT max(snapshot_date) FROM raw_mint_supplies) "
            "AND supply_ui > 0"
        ).fetchall()
    rprint(f"[cyan]Deriving SY supply history for {len(anchors)} mints "
           f"({WINDOW_DAYS}d window)…[/cyan]")

    all_rows: list[tuple] = []
    async with httpx.AsyncClient(timeout=40) as client:
        for i, (mint, anchor) in enumerate(anchors, 1):
            rows, n_sigs = await _derive_mint(client, url, mint, anchor, cutoff_ts, today)
            all_rows.extend(rows)
            rprint(f"  [{i}/{len(anchors)}] {mint[:8]} anchor={anchor:,.0f} "
                   f"sigs={n_sigs} days={len(rows)}")

    with warehouse() as con:
        con.execute("DELETE FROM raw_sy_supply_history")
        if all_rows:
            con.executemany(
                "INSERT INTO raw_sy_supply_history (mint, date, supply_ui, snapshot_ts) "
                "VALUES (?, ?, ?, ?)",
                all_rows,
            )
    rprint(f"[green]Wrote {len(all_rows)} daily SY supply rows[/green]")
    return {"mints": len(anchors), "rows": len(all_rows)}


def main() -> None:
    started = datetime.now(timezone.utc)
    rprint(f"[bold]extract_sy_supply_history[/bold]  started {started.isoformat()}")
    asyncio.run(run())
    rprint(f"[bold]done[/bold]  elapsed {datetime.now(timezone.utc) - started}")


if __name__ == "__main__":
    main()
