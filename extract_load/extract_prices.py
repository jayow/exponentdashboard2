"""Daily USD price extraction.

Mirrors Exponent's own price-source choices. Exponent's /markets API tags
each token with a `price_source` field: "pyth" (for SOL, JitoSOL) or
"jupiter" (for everything else). We use the same two sources at the same
endpoints — Pyth Hermes/benchmarks and Jupiter datapi.

Sources of truth:
  - Jupiter datapi: https://datapi.jup.ag/v2/charts/{mint}
      ?interval=1_DAY&type=price&candles=N&to=<MILLISECONDS>
      Returns OHLCV daily candles. Discovered by mining Jupiter's
      tradingview chunk (jup.ag/_next/static/chunks/lib-tradingview-*.js).
      Free, no auth, generous rate.
  - Pyth Hermes benchmarks: https://benchmarks.pyth.network/v1/shims/tradingview/history
      ?symbol=Crypto.SOL/USD&resolution=D&from=<sec>&to=<sec>
      Same on-chain oracle data. 1-year max window per request — chunk.

For tokens we discover at runtime (long-tail expired markets), we default
to Jupiter unless the symbol resolves to a known Pyth feed.
"""
from __future__ import annotations
import asyncio
import time
from datetime import datetime, timezone, date as Date
from typing import Any

import duckdb
import httpx
from rich import print as rprint
from rich.progress import (
    BarColumn,
    MofNCompleteColumn,
    Progress,
    SpinnerColumn,
    TextColumn,
    TimeElapsedColumn,
)

from .load import warehouse


JUP_CHART_URL = "https://datapi.jup.ag/v2/charts"
PYTH_HISTORY_URL = "https://benchmarks.pyth.network/v1/shims/tradingview/history"
PYTH_FEEDS_URL = "https://hermes.pyth.network/v2/price_feeds"

# Symbol -> Pyth TradingView symbol mapping. Built from observation that
# Exponent uses Pyth for SOL + JitoSOL specifically. Add more as discovered.
PYTH_FEEDS_BY_SYMBOL: dict[str, str] = {
    "SOL":     "Crypto.SOL/USD",
    "JitoSOL": "Crypto.JITOSOL/USD",
    "USDC":    "Crypto.USDC/USD",
    "USDT":    "Crypto.USDT/USD",
    "BTC":     "Crypto.BTC/USD",
    "cbBTC":   "Crypto.CBBTC/USD",
    "ETH":     "Crypto.ETH/USD",
    "JTO":     "Crypto.JTO/USD",
    "JUP":     "Crypto.JUP/USD",
    "INF":     "Crypto.INF/USD",
    "mSOL":    "Crypto.MSOL/USD",
    "bSOL":    "Crypto.BSOL/USD",
}

# Symbols that are stable enough to assume = $1 when no other source is
# available (pre-Jupiter-coverage period for stablecoins).
PEGGED_USD_SYMBOLS: set[str] = {
    "USD", "USDC", "USDT", "PYUSD", "USDe", "USDS", "USDG",
    "USX", "eUSX", "USD*", "USDC+",
    # Yield-bearing stables — also USD-pegged but with slight premium; close
    # enough to fall back to 1.0 if no Jupiter data exists yet.
    "hyUSD", "sHYUSD", "sUSDe", "syrupUSDC",
    "kUSDC", "mUSDC", "mUSDT", "kPYUSD", "mPYUSD",
    "jlUSDG", "wYLDS",
}


def _now_ms() -> int:
    return int(datetime.now(timezone.utc).timestamp() * 1000)


def _to_date(unix_seconds: int) -> Date:
    return datetime.fromtimestamp(unix_seconds, tz=timezone.utc).date()


async def fetch_jupiter_candles(
    client: httpx.AsyncClient, mint: str, candles: int = 600
) -> list[dict]:
    """Daily OHLCV candles from Jupiter datapi. Up to 600 candles back from now."""
    r = await client.get(
        f"{JUP_CHART_URL}/{mint}",
        params={
            "interval": "1_DAY",
            "type": "price",
            "candles": candles,
            "to": _now_ms(),
        },
        timeout=30,
    )
    r.raise_for_status()
    body = r.json()
    return body.get("candles") or []


async def fetch_pyth_history(
    client: httpx.AsyncClient, symbol: str, from_ts: int, to_ts: int
) -> list[dict]:
    """Daily candles from Pyth benchmarks (TradingView shape).
    Chunks the request by 1-year windows.
    """
    out: list[dict] = []
    year = 365 * 24 * 3600
    cur = from_ts
    while cur < to_ts:
        chunk_to = min(cur + year - 1, to_ts)
        r = await client.get(
            PYTH_HISTORY_URL,
            params={"symbol": symbol, "resolution": "D", "from": cur, "to": chunk_to},
            timeout=30,
        )
        if r.status_code != 200:
            break
        body = r.json()
        if body.get("s") != "ok":
            # 'no_data' or 'error' — stop chunking
            break
        ts = body.get("t") or []
        o = body.get("o") or []
        h = body.get("h") or []
        low = body.get("l") or []
        c = body.get("c") or []
        v = body.get("v") or []
        for i, t in enumerate(ts):
            out.append({
                "time": t,
                "open":  o[i] if i < len(o) else None,
                "high":  h[i] if i < len(h) else None,
                "low":   low[i] if i < len(low) else None,
                "close": c[i] if i < len(c) else None,
                "volume": v[i] if i < len(v) else None,
            })
        cur = chunk_to + 1
    return out


def _flush_rows(
    con: duckdb.DuckDBPyConnection, mint: str, source: str, candles: list[dict]
) -> int:
    """Upsert daily price rows for a mint. Returns count inserted."""
    if not candles:
        return 0
    rows = []
    for c in candles:
        if c.get("close") is None:
            continue
        d = _to_date(c["time"])
        rows.append((
            mint, d,
            c.get("close"), c.get("open"), c.get("high"), c.get("low"), c.get("volume"),
            source,
        ))
    if not rows:
        return 0
    con.executemany(
        """
        INSERT INTO raw_prices (mint, date, close_usd, open_usd, high_usd, low_usd, volume_usd, source, fetched_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
        ON CONFLICT (mint, date) DO UPDATE SET
            close_usd  = excluded.close_usd,
            open_usd   = excluded.open_usd,
            high_usd   = excluded.high_usd,
            low_usd    = excluded.low_usd,
            volume_usd = excluded.volume_usd,
            source     = excluded.source,
            fetched_at = excluded.fetched_at
        """,
        rows,
    )
    return len(rows)


def _enumerate_targets(con: duckdb.DuckDBPyConnection) -> list[tuple[str, str | None]]:
    """Enumerate distinct (mint, symbol_hint) pairs we need pricing for.

    Sources:
      - dim_markets.underlying_mint — v1 markets' underlying tokens
      - raw_lst_rates.base_mint     — base tokens that LST rates derive from
        (e.g. SLX is needed to price stSLX, USDG for kUSDG). These don't
        always show up as a market underlying directly.

    Symbol comes from dim_markets.ticker or raw_token_metadata.symbol — used
    only for routing to Pyth vs Jupiter (and the PEGGED_USD_SYMBOLS fallback).
    """
    rows = con.execute(
        """
        SELECT DISTINCT mint, sym FROM (
          SELECT m.underlying_mint AS mint,
                 COALESCE(m.ticker, tm.symbol, et.symbol) AS sym
          FROM main_core.dim_markets m
          LEFT JOIN raw_token_metadata tm ON tm.mint = m.underlying_mint
          LEFT JOIN raw_exponent_tokens et ON et.mint = m.underlying_mint
          WHERE m.underlying_mint IS NOT NULL

          UNION ALL

          -- Base mints needed to compute LST USD prices via raw_lst_rates.
          -- If we don't price these, stSLX/kUSDG/etc. stay $0 because
          -- int_lst_prices joins on base_mint.
          SELECT l.base_mint AS mint, tm.symbol AS sym
          FROM raw_lst_rates l
          LEFT JOIN raw_token_metadata tm ON tm.mint = l.base_mint
        )
        WHERE mint IS NOT NULL
        ORDER BY 1
        """
    ).fetchall()
    return [(r[0], r[1]) for r in rows]


def _fill_pegged_stables(
    con: duckdb.DuckDBPyConnection, mint: str, symbol: str | None
) -> int:
    """For pegged-USD symbols, ensure every date in raw_signatures range has a
    price row at $1.0 (source='stable'). This fills the pre-Jupiter-coverage gap
    for stablecoins. Where Jupiter (or Pyth) ALSO has a real price for the same
    date, the real source wins via the ON CONFLICT — we insert with priority logic
    in stg_prices, not here, so we just insert source='stable' rows here unconditionally
    and let staging pick the best source.
    """
    if not symbol or symbol not in PEGGED_USD_SYMBOLS:
        return 0
    # Get the date range for this mint based on swap activity.
    bounds = con.execute(
        """
        SELECT MIN(date), MAX(date)
        FROM main_intermediate.int_amm_swaps
        WHERE underlying_mint = ?
        """,
        [mint],
    ).fetchone()
    if not bounds or not bounds[0]:
        return 0
    start, end = bounds
    rows = con.execute(
        "SELECT generate_series::DATE FROM generate_series(?::DATE, ?::DATE, INTERVAL 1 DAY)",
        [start, end],
    ).fetchall()
    payload = [
        (mint, r[0], 1.0, 1.0, 1.0, 1.0, None, "stable") for r in rows
    ]
    if not payload:
        return 0
    con.executemany(
        """
        INSERT INTO raw_prices (mint, date, close_usd, open_usd, high_usd, low_usd, volume_usd, source)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT (mint, date) DO NOTHING
        """,
        payload,
    )
    return len(payload)


async def run() -> dict:
    started = datetime.now(timezone.utc)
    stats = {"jupiter": 0, "pyth": 0, "stable": 0, "tokens": 0, "skipped": 0}

    with warehouse() as con:
        targets = _enumerate_targets(con)
        rprint(
            f"[bold]extract_prices[/bold]  {len(targets)} unique underlying mints to price"
        )

        async with httpx.AsyncClient(
            timeout=30,
            headers={"User-Agent": "Mozilla/5.0"},
        ) as client:
            with Progress(
                SpinnerColumn(),
                TextColumn("[progress.description]{task.description}"),
                BarColumn(),
                MofNCompleteColumn(),
                TimeElapsedColumn(),
            ) as bar:
                task = bar.add_task("pricing", total=len(targets))

                # We need an earliest-date for Pyth chunking — use raw_signatures.
                bounds = con.execute(
                    "SELECT MIN(block_time), MAX(block_time) FROM raw_signatures WHERE block_time IS NOT NULL"
                ).fetchone()
                earliest_ts = int(bounds[0]) if bounds and bounds[0] else int(time.time()) - 365 * 24 * 3600
                now_ts = int(time.time())

                for mint, symbol in targets:
                    pyth_sym = PYTH_FEEDS_BY_SYMBOL.get(symbol or "")
                    if pyth_sym:
                        try:
                            candles = await fetch_pyth_history(
                                client, pyth_sym, earliest_ts, now_ts
                            )
                            n = _flush_rows(con, mint, "pyth", candles)
                            if n > 0:
                                stats["pyth"] += n
                                stats["tokens"] += 1
                                bar.update(task, advance=1)
                                continue
                        except Exception as e:
                            rprint(f"  [yellow]pyth fail[/yellow] {symbol}: {e}")
                    # Fall through to Jupiter
                    try:
                        candles = await fetch_jupiter_candles(client, mint, candles=600)
                        n = _flush_rows(con, mint, "jupiter", candles)
                        if n > 0:
                            stats["jupiter"] += n
                            stats["tokens"] += 1
                    except Exception as e:
                        rprint(f"  [yellow]jupiter fail[/yellow] {symbol or mint[:8]}: {e}")
                        stats["skipped"] += 1
                    # Pegged stable fallback for any gaps
                    n_stable = _fill_pegged_stables(con, mint, symbol)
                    stats["stable"] += n_stable
                    bar.update(task, advance=1)

    rprint(
        f"[green]Done[/green]  tokens={stats['tokens']}  "
        f"jupiter={stats['jupiter']:,}  pyth={stats['pyth']:,}  "
        f"stable={stats['stable']:,}  skipped={stats['skipped']}"
    )
    rprint(f"[bold]elapsed[/bold] {datetime.now(timezone.utc) - started}")
    return stats


def main() -> None:
    asyncio.run(run())


if __name__ == "__main__":
    main()
