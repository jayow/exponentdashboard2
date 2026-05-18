"""Query DuckDB marts → emit slim per-tab JSONs into web/public/.

Replaces v1's monolithic analytics.json with per-tab files. Each writes
atomically (tmp file + rename) so the frontend never sees a half-written file.

Today this produces:
  - volume.json    daily trading volume series (protocol + by-market + by-side)

Future:
  - overview.json
  - markets.json
  - holders.json
  - etc.
"""
from __future__ import annotations
import json
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path

import duckdb
from rich import print as rprint

from extract_load.config import WAREHOUSE_PATH, ROOT


WEB_PUBLIC = ROOT / "web" / "public"


def _write_atomic(path: Path, payload: dict | list) -> None:
    """Write JSON atomically: write to tmp then rename."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        mode="w", dir=path.parent, suffix=".tmp", delete=False, encoding="utf-8"
    ) as f:
        json.dump(payload, f, separators=(",", ":"))
        tmp_name = f.name
    os.replace(tmp_name, path)


def build_volume_json(con: duckdb.DuckDBPyConnection) -> dict:
    """Trading volume time-series.

    Shape:
      {
        meta: { generatedAt, dateRange, totals, source },
        dates: [YYYY-MM-DD, ...],
        protocol: { pt: [...], yt: [...], total: [...] },
        byMarket: { <marketKey>: { ticker, dates, pt, yt, total }, ... },
        topMarkets: [ {marketKey, ticker, total} ]  // sorted desc, for quick chips
      }
    """
    # Date range
    date_row = con.execute(
        "SELECT MIN(date)::VARCHAR, MAX(date)::VARCHAR FROM main_analytics.trading_volume_daily"
    ).fetchone()
    if not date_row or not date_row[0]:
        return {"meta": {"generatedAt": datetime.now(timezone.utc).isoformat(), "empty": True}}

    min_d, max_d = date_row
    # Build a continuous date axis (so charts have no gaps)
    dates = [
        r[0]
        for r in con.execute(
            f"""
            SELECT generate_series::DATE::VARCHAR FROM
              generate_series(DATE '{min_d}', DATE '{max_d}', INTERVAL 1 DAY)
            """
        ).fetchall()
    ]

    # Protocol-wide daily totals per side. Emit both USD and underlying-unit
    # variants so the frontend can toggle.
    proto_rows = con.execute(
        """
        SELECT date::VARCHAR,
               COALESCE(SUM(volume_usd)         FILTER (WHERE side = 'PT'), 0) AS pt_usd,
               COALESCE(SUM(volume_usd)         FILTER (WHERE side = 'YT'), 0) AS yt_usd,
               COALESCE(SUM(volume_underlying)  FILTER (WHERE side = 'PT'), 0) AS pt_und,
               COALESCE(SUM(volume_underlying)  FILTER (WHERE side = 'YT'), 0) AS yt_und
        FROM main_analytics.trading_volume_daily
        GROUP BY 1 ORDER BY 1
        """
    ).fetchall()
    by_date = {r[0]: (float(r[1]), float(r[2]), float(r[3]), float(r[4])) for r in proto_rows}
    pt_usd  = [by_date.get(d, (0.0, 0.0, 0.0, 0.0))[0] for d in dates]
    yt_usd  = [by_date.get(d, (0.0, 0.0, 0.0, 0.0))[1] for d in dates]
    pt_und  = [by_date.get(d, (0.0, 0.0, 0.0, 0.0))[2] for d in dates]
    yt_und  = [by_date.get(d, (0.0, 0.0, 0.0, 0.0))[3] for d in dates]
    total_usd = [a + b for a, b in zip(pt_usd, yt_usd)]
    total_und = [a + b for a, b in zip(pt_und, yt_und)]

    # Per-market series + top (USD)
    per_market_rows = con.execute(
        """
        SELECT market_key, ticker, date::VARCHAR, side,
               SUM(volume_usd) AS v_usd, SUM(volume_underlying) AS v_und
        FROM main_analytics.trading_volume_daily
        GROUP BY 1, 2, 3, 4
        """
    ).fetchall()
    by_market: dict[str, dict] = {}
    for market_key, ticker, date, side, v_usd, v_und in per_market_rows:
        entry = by_market.setdefault(
            market_key,
            {
                "ticker": ticker,
                "pt_usd_map": {}, "yt_usd_map": {},
                "pt_und_map": {}, "yt_und_map": {},
            },
        )
        if side == "PT":
            entry["pt_usd_map"][date] = float(v_usd or 0)
            entry["pt_und_map"][date] = float(v_und or 0)
        elif side == "YT":
            entry["yt_usd_map"][date] = float(v_usd or 0)
            entry["yt_und_map"][date] = float(v_und or 0)

    by_market_out: dict[str, dict] = {}
    market_totals: list[tuple[str, str, float]] = []
    for market_key, entry in by_market.items():
        pt_usd_arr = [entry["pt_usd_map"].get(d, 0.0) for d in dates]
        yt_usd_arr = [entry["yt_usd_map"].get(d, 0.0) for d in dates]
        pt_und_arr = [entry["pt_und_map"].get(d, 0.0) for d in dates]
        yt_und_arr = [entry["yt_und_map"].get(d, 0.0) for d in dates]
        total_usd_arr = [a + b for a, b in zip(pt_usd_arr, yt_usd_arr)]
        total_und_arr = [a + b for a, b in zip(pt_und_arr, yt_und_arr)]
        total_usd_sum = sum(total_usd_arr)
        by_market_out[market_key] = {
            "ticker": entry["ticker"],
            "ptUsd": pt_usd_arr,
            "ytUsd": yt_usd_arr,
            "totalUsd": total_usd_arr,
            "ptUnderlying": pt_und_arr,
            "ytUnderlying": yt_und_arr,
            "totalUnderlying": total_und_arr,
        }
        market_totals.append((market_key, entry["ticker"] or "", total_usd_sum))

    market_totals.sort(key=lambda x: -x[2])
    top = [{"marketKey": mk, "ticker": tk, "totalUsd": tot} for mk, tk, tot in market_totals[:20]]

    payload = {
        "meta": {
            "generatedAt": datetime.now(timezone.utc).isoformat(),
            "dateRange": [min_d, max_d],
            "totalsUsd": {
                "pt": sum(pt_usd),
                "yt": sum(yt_usd),
                "total": sum(total_usd),
            },
            "totalsUnderlying": {
                "pt": sum(pt_und),
                "yt": sum(yt_und),
                "total": sum(total_und),
            },
            "source": "trading_volume_daily",
            "priceSources": "pyth+jupiter (matches Exponent's price_source)",
        },
        "dates": dates,
        "protocol": {
            "ptUsd": pt_usd, "ytUsd": yt_usd, "totalUsd": total_usd,
            "ptUnderlying": pt_und, "ytUnderlying": yt_und, "totalUnderlying": total_und,
        },
        "byMarket": by_market_out,
        "topMarkets": top,
    }
    return payload


def build() -> None:
    WEB_PUBLIC.mkdir(parents=True, exist_ok=True)
    con = duckdb.connect(str(WAREHOUSE_PATH), read_only=True)
    try:
        rprint("[cyan]Building volume.json…[/cyan]")
        payload = build_volume_json(con)
        _write_atomic(WEB_PUBLIC / "volume.json", payload)
        meta = payload.get("meta") or {}
        usd = meta.get("totalsUsd") or {}
        rprint(
            f"  wrote volume.json  range={meta.get('dateRange')}  "
            f"PT_USD={usd.get('pt', 0):,.0f}  YT_USD={usd.get('yt', 0):,.0f}"
        )
    finally:
        con.close()
    rprint("[green]serve done[/green]")


if __name__ == "__main__":
    build()
