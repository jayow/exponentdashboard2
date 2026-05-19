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
import re
import tempfile
from datetime import datetime, timezone
from pathlib import Path

import duckdb
from rich import print as rprint

from extract_load.config import WAREHOUSE_PATH, ROOT


WEB_PUBLIC = ROOT / "web" / "public"


# Platform consolidation (mirrors v1's normalize_platform). The raw platform
# strings from the Exponent API are fine-grained (e.g. "Hylo Staked SOL",
# "Hylo USD", "Jito Restaking") — for the dashboard we want the brand.
_PLATFORM_RULES = [
    (re.compile(r"^Hylo", re.I),            "Hylo"),
    (re.compile(r"^Drift", re.I),           "Drift"),
    (re.compile(r"^Jupiter", re.I),         "Jupiter"),
    (re.compile(r"^Jito Restaking", re.I),  "Fragmetric"),
    (re.compile(r"^Jito", re.I),            "Jito"),
    (re.compile(r"^BULK", re.I),            "BULK"),
]

# Ticker → platform fallback for markets where dim_markets.platform is null
# (typically expired markets discovered only via Metaplex name pattern).
_TICKER_TO_PLATFORM = {
    "USX": "Solstice", "eUSX": "Solstice",
    "ONyc": "OnRe",
    "BulkSOL": "BULK", "wBulkSOL": "BULK",
    "hyloSOL": "Hylo", "hyloSOL+": "Hylo", "hySOL+": "Hylo", "hyUSD": "Hylo",
    "sHYUSD": "Hylo", "xSOL": "Hylo",
    "fragSOL": "Fragmetric", "fragBTC": "Fragmetric", "wfragBTC": "Fragmetric",
    "JitoSOL": "Jito",
    "JLP": "Jupiter", "jlUSDG": "Jupiter", "jlSOL": "Jupiter",
    "kySOL": "Kyros",
    "dSOL": "Drift", "dzSOL": "Drift", "dfdvSOL": "Drift",
    "INF": "Sanctum",
    "rkuSOL": "Kuru",
    "CRT": "Carrot",
    "stORE": "Ore",
    "USDe": "Ethena", "sUSDe": "Ethena",
    "USDC+": "Perena", "mUSDC": "Perena", "kUSDC": "Perena",
    "USD*": "USD*",
    "MLP": "MarginFi", "ALP": "Asgard",
    "syUSDC": "Solend",
}


def normalize_platform(raw: str | None, ticker: str | None = None) -> str:
    """Apply v1's regex normalization, with a ticker-based fallback for
    expired/unlabeled markets."""
    if raw:
        for pat, name in _PLATFORM_RULES:
            if pat.match(raw):
                return name
        return raw
    if ticker and ticker in _TICKER_TO_PLATFORM:
        return _TICKER_TO_PLATFORM[ticker]
    return "Other"


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

    # Per-platform volume series — need to resolve ticker → platform.
    # Pull the (market_key → ticker → platform) mapping from dim_markets +
    # ticker fallback, then sum each market's total series into its platform.
    plat_lookup_rows = con.execute("""
        SELECT market_key, ticker, platform FROM main_core.dim_markets
    """).fetchall()
    platform_for_mk: dict[str, str] = {}
    for mk, ticker, plat in plat_lookup_rows:
        platform_for_mk[mk] = normalize_platform(plat, ticker)
    by_platform_usd: dict[str, list[float]] = {}
    for mk, entry in by_market_out.items():
        plat = platform_for_mk.get(mk) or normalize_platform(None, entry["ticker"])
        if plat not in by_platform_usd:
            by_platform_usd[plat] = [0.0] * len(dates)
        for i, v in enumerate(entry["totalUsd"]):
            by_platform_usd[plat][i] += v
    by_platform_sorted = dict(sorted(
        by_platform_usd.items(),
        key=lambda kv: (-sum(kv[1]), kv[0])
    ))

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
        "byPlatform": by_platform_sorted,
        "byMarket": by_market_out,
        "topMarkets": top,
    }
    return payload


def build_tvl_json(con: duckdb.DuckDBPyConnection) -> dict:
    """Daily TVL series — protocol total + per-market USD + underlying-units."""
    bounds = con.execute("SELECT MIN(date)::VARCHAR, MAX(date)::VARCHAR FROM main_analytics.tvl_daily").fetchone()
    if not bounds or not bounds[0]:
        return {"meta": {"generatedAt": datetime.now(timezone.utc).isoformat(), "empty": True}}
    min_d, max_d = bounds
    dates = [r[0] for r in con.execute(
        f"SELECT generate_series::DATE::VARCHAR FROM generate_series(DATE '{min_d}', DATE '{max_d}', INTERVAL 1 DAY)"
    ).fetchall()]
    # Protocol total per day — both SY-based (headline) and PT-based (principal/active)
    proto = con.execute("""
        SELECT date::VARCHAR,
               SUM(tvl_usd),
               SUM(principal_tvl_usd),
               SUM(tvl_underlying)
        FROM main_analytics.tvl_daily GROUP BY 1 ORDER BY 1
    """).fetchall()
    proto_map = {r[0]: (float(r[1] or 0), float(r[2] or 0), float(r[3] or 0)) for r in proto}
    proto_usd      = [proto_map.get(d, (0, 0, 0))[0] for d in dates]
    proto_principal = [proto_map.get(d, (0, 0, 0))[1] for d in dates]
    # Per-market series — load raw platform too so we can roll up byPlatform
    rows = con.execute("""
        SELECT t.market_key, t.ticker, t.platform, t.date::VARCHAR,
               t.tvl_usd, t.tvl_underlying, t.principal_tvl_usd
        FROM main_analytics.tvl_daily t
    """).fetchall()
    by_market: dict[str, dict] = {}
    platform_for_mk: dict[str, str] = {}
    for mk, ticker, raw_plat, date, tvl_usd, tvl_und, principal in rows:
        e = by_market.setdefault(mk, {"ticker": ticker, "platform": normalize_platform(raw_plat, ticker),
                                      "usd_map": {}, "und_map": {}, "principal_map": {}})
        platform_for_mk[mk] = e["platform"]
        e["usd_map"][date] = float(tvl_usd or 0)
        e["und_map"][date] = float(tvl_und or 0)
        e["principal_map"][date] = float(principal or 0)
    by_market_out: dict[str, dict] = {}
    totals = []
    for mk, e in by_market.items():
        usd = [e["usd_map"].get(d, 0.0) for d in dates]
        und = [e["und_map"].get(d, 0.0) for d in dates]
        principal = [e["principal_map"].get(d, 0.0) for d in dates]
        by_market_out[mk] = {
            "ticker": e["ticker"],
            "platform": e["platform"],
            "tvlUsd": usd,
            "tvlUnderlying": und,
            "principalUsd": principal,
        }
        totals.append((mk, e["ticker"] or "", usd[-1] if usd else 0))
    totals.sort(key=lambda x: -x[2])
    top = [{"marketKey": mk, "ticker": tk, "tvlUsdNow": v} for mk, tk, v in totals[:20]]

    # Per-platform daily timeseries — sum each market's series into its platform
    by_platform: dict[str, list[float]] = {}
    for mk, m in by_market_out.items():
        plat = m["platform"]
        if plat not in by_platform:
            by_platform[plat] = [0.0] * len(dates)
        for i, v in enumerate(m["tvlUsd"]):
            by_platform[plat][i] += v
    # Sort platforms by latest value, place "Other" last
    platforms_sorted = sorted(
        by_platform.items(),
        key=lambda kv: (-kv[1][-1] if kv[1] else 0, kv[0])
    )

    # Decomposition: PT (principal) + LP + Idle = SY total per day
    # Pull LP USD value (from active_positions_daily) and SY USD value
    # (from int_sy_tvl_daily, summed across distinct sy mints per date).
    decomp = con.execute("""
        WITH lp AS (
          SELECT date, SUM(usd_value) AS lp_usd
          FROM main_analytics.active_positions_daily
          WHERE leg = 'LP'
          GROUP BY date
        ),
        sy AS (
          SELECT date, SUM(tvl_usd) AS sy_usd FROM main_intermediate.int_sy_tvl_daily GROUP BY date
        ),
        pt AS (
          SELECT date, SUM(principal_tvl_usd) AS pt_usd FROM main_analytics.tvl_daily GROUP BY date
        )
        SELECT sy.date::VARCHAR, sy.sy_usd, COALESCE(pt.pt_usd,0), COALESCE(lp.lp_usd,0)
        FROM sy LEFT JOIN pt USING (date) LEFT JOIN lp USING (date)
    """).fetchall()
    decomp_map = {r[0]: (float(r[1] or 0), float(r[2] or 0), float(r[3] or 0)) for r in decomp}
    pt_arr = [decomp_map.get(d, (0,0,0))[1] for d in dates]
    lp_arr = [decomp_map.get(d, (0,0,0))[2] for d in dates]
    # Idle = SY_total - PT - LP, floored at 0 to absorb attribution noise
    idle_arr = [
        max(0.0, decomp_map.get(d, (0,0,0))[0] - decomp_map.get(d, (0,0,0))[1] - decomp_map.get(d, (0,0,0))[2])
        for d in dates
    ]

    return {
        "meta": {
            "generatedAt": datetime.now(timezone.utc).isoformat(),
            "dateRange": [min_d, max_d],
            "currentTvlUsd": proto_usd[-1] if proto_usd else 0,
            "currentPrincipalUsd": proto_principal[-1] if proto_principal else 0,
            "currentLpUsd": lp_arr[-1] if lp_arr else 0,
            "currentIdleUsd": idle_arr[-1] if idle_arr else 0,
            "source": "tvl_daily — SY_supply × SY_rate × underlying_USD (DefiLlama-compatible). Principal series = PT_supply × underlying_USD.",
        },
        "dates": dates,
        "protocolUsd": proto_usd,
        "protocolPrincipalUsd": proto_principal,
        "decomposition": {
            "principalPt": pt_arr,
            "liquidityLp": lp_arr,
            "idle":        idle_arr,
        },
        "byPlatform": {plat: series for plat, series in platforms_sorted},
        "byMarket": by_market_out,
        "topMarkets": top,
    }


def build_market_share_json(con: duckdb.DuckDBPyConnection) -> dict:
    """Per-asset (ticker) market share — full daily timeseries + latest
    snapshot + 30d rollup.

    The timeseries enables a stacked-bar dominance view (which ticker
    captured the most volume / TVL each day)."""
    bounds = con.execute(
        "SELECT MIN(date)::VARCHAR, MAX(date)::VARCHAR FROM main_analytics.market_share_daily WHERE ticker IS NOT NULL"
    ).fetchone()
    if not bounds or not bounds[0]:
        return {"meta": {"generatedAt": datetime.now(timezone.utc).isoformat(), "empty": True}}
    min_d, max_d = bounds
    dates = [r[0] for r in con.execute(
        f"SELECT generate_series::DATE::VARCHAR FROM generate_series(DATE '{min_d}', DATE '{max_d}', INTERVAL 1 DAY)"
    ).fetchall()]
    # Per-ticker daily series (volume USD + tvl USD)
    ts_rows = con.execute("""
        SELECT date::VARCHAR, ticker, volume_usd, tvl_usd
        FROM main_analytics.market_share_daily
        WHERE ticker IS NOT NULL
    """).fetchall()
    by_ticker_vol: dict[str, dict[str, float]] = {}
    by_ticker_tvl: dict[str, dict[str, float]] = {}
    for date, ticker, v, t in ts_rows:
        if v: by_ticker_vol.setdefault(ticker, {})[date] = float(v)
        if t: by_ticker_tvl.setdefault(ticker, {})[date] = float(t)
    # Densify into arrays aligned to `dates`
    all_tickers = sorted(set(by_ticker_vol.keys()) | set(by_ticker_tvl.keys()))
    timeseries = {}
    for ticker in all_tickers:
        timeseries[ticker] = {
            "volumeUsd": [by_ticker_vol.get(ticker, {}).get(d, 0.0) for d in dates],
            "tvlUsd":    [by_ticker_tvl.get(ticker, {}).get(d, 0.0) for d in dates],
        }
    # Latest snapshot
    rows = con.execute("""
        WITH latest AS (SELECT MAX(date) AS d FROM main_analytics.market_share_daily)
        SELECT ms.ticker, ms.volume_usd, ms.volume_share_pct, ms.tvl_usd, ms.tvl_share_pct
        FROM main_analytics.market_share_daily ms JOIN latest l ON ms.date = l.d
        WHERE ms.ticker IS NOT NULL
        ORDER BY COALESCE(ms.tvl_usd, 0) DESC NULLS LAST
    """).fetchall()
    snapshot = [
        {"ticker": r[0], "volumeUsd": float(r[1] or 0), "volumeSharePct": float(r[2] or 0),
         "tvlUsd": float(r[3] or 0), "tvlSharePct": float(r[4] or 0)}
        for r in rows
    ]
    # 30-day rollup
    rows30 = con.execute("""
        SELECT ticker, SUM(volume_usd) AS vol30, AVG(tvl_usd) AS tvl_avg30
        FROM main_analytics.market_share_daily
        WHERE date >= CURRENT_DATE - INTERVAL 30 DAY AND ticker IS NOT NULL
        GROUP BY 1
    """).fetchall()
    total_vol30 = sum(r[1] or 0 for r in rows30) or 1
    total_tvl30 = sum(r[2] or 0 for r in rows30) or 1
    rollup30 = [
        {"ticker": r[0], "volumeUsd30d": float(r[1] or 0),
         "volumeShare30dPct": 100.0 * (r[1] or 0) / total_vol30,
         "tvlUsdAvg30d": float(r[2] or 0),
         "tvlShare30dPct": 100.0 * (r[2] or 0) / total_tvl30}
        for r in rows30
    ]
    rollup30.sort(key=lambda x: -x["tvlUsdAvg30d"])
    return {
        "meta": {
            "generatedAt": datetime.now(timezone.utc).isoformat(),
            "dateRange": [min_d, max_d],
            "tickers": all_tickers,
        },
        "dates": dates,
        "byTicker": timeseries,
        "snapshot": snapshot,
        "rolling30d": rollup30,
    }


def build_active_positions_json(con: duckdb.DuckDBPyConnection) -> dict:
    """Active position supplies (PT/YT/LP/SY) in UNDERLYING units, indexed by ticker.

    Shape:
      meta: {generatedAt, dateRange, tickers: [{ticker, marketCount}]}
      dates: [...]
      byTicker:
        USX:
          underlyingMint, latest: {pt, yt, lp, sy} (sums for the rollup row)
          legs:
            PT: {byMarket: {USX-01JUN26: [supplies...], ...}, totals: [...]}
            YT: same
            LP: same
            SY: {totals: [...]}  // SY is mint-deduped, no per-market breakdown
    """
    bounds = con.execute(
        "SELECT MIN(date)::VARCHAR, MAX(date)::VARCHAR FROM main_analytics.active_positions_daily"
    ).fetchone()
    if not bounds or not bounds[0]:
        return {"meta": {"generatedAt": datetime.now(timezone.utc).isoformat(), "empty": True}}
    min_d, max_d = bounds
    dates = [r[0] for r in con.execute(
        f"SELECT generate_series::DATE::VARCHAR FROM generate_series(DATE '{min_d}', DATE '{max_d}', INTERVAL 1 DAY)"
    ).fetchall()]
    rows = con.execute("""
        SELECT date::VARCHAR, ticker, leg, market_key, underlying_mint, supply
        FROM main_analytics.active_positions_daily
        WHERE ticker IS NOT NULL
    """).fetchall()
    # Build: byTicker[ticker][leg] -> {byMarket: {mk: {date: supply}}, totals: {date: supply}}
    by_ticker: dict[str, dict] = {}
    for date, ticker, leg, mk, und, supply in rows:
        t = by_ticker.setdefault(ticker, {"underlyingMint": und, "legs": {}})
        legd = t["legs"].setdefault(leg, {"byMarket": {}, "totalsMap": {}})
        if mk is not None:
            legd["byMarket"].setdefault(mk, {})[date] = float(supply or 0)
        legd["totalsMap"][date] = legd["totalsMap"].get(date, 0.0) + float(supply or 0)
        if und and not t.get("underlyingMint"):
            t["underlyingMint"] = und
    # Densify into arrays aligned to `dates`
    ticker_list = []
    for ticker, t in by_ticker.items():
        out_legs: dict[str, dict] = {}
        for leg, legd in t["legs"].items():
            by_market_arr = {
                mk: [series.get(d, 0.0) for d in dates]
                for mk, series in legd["byMarket"].items()
            }
            totals_arr = [legd["totalsMap"].get(d, 0.0) for d in dates]
            out_legs[leg] = {"byMarket": by_market_arr, "totals": totals_arr}
        t["legs"] = out_legs
        latest = {
            leg: (legd["totals"][-1] if legd["totals"] else 0.0)
            for leg, legd in out_legs.items()
        }
        t["latest"] = latest
        ticker_list.append({
            "ticker": ticker,
            "marketCount": sum(1 for leg in ("PT",) for _ in out_legs.get(leg, {}).get("byMarket", {})),
            "latestTotal": sum(latest.values()),
        })
    ticker_list.sort(key=lambda x: -x["latestTotal"])
    return {
        "meta": {
            "generatedAt": datetime.now(timezone.utc).isoformat(),
            "dateRange": [min_d, max_d],
            "tickers": [{"ticker": t["ticker"], "marketCount": t["marketCount"]} for t in ticker_list],
        },
        "dates": dates,
        "byTicker": by_ticker,
    }


def build_stats_json(con: duckdb.DuckDBPyConnection) -> dict:
    """Headline protocol stats — single shot, all on-chain derived.

    Fields:
      activeMarkets, expiredMarkets, totalMarkets, platforms, tickers
      peakTvlUsd / peakTvlDate
      currentTvlUsd / currentPrincipalUsd
      lifetimeVolumeUsd / volume30dUsd
      protocolAgeDays (since first observed market activity)
      firstActivityDate, latestMaturityDate
    """
    today = con.execute("SELECT CURRENT_DATE").fetchone()[0]
    mk = con.execute("""
        SELECT
          COUNT(*) FILTER (WHERE maturity_date >= CURRENT_DATE)         AS active,
          COUNT(*) FILTER (WHERE maturity_date <  CURRENT_DATE)         AS expired,
          COUNT(*)                                                      AS total,
          COUNT(DISTINCT platform)                                      AS platforms,
          COUNT(DISTINCT ticker)                                        AS tickers,
          MAX(maturity_date)                                            AS latest_maturity
        FROM main_core.dim_markets
        WHERE maturity_date IS NOT NULL
    """).fetchone()
    # Peak TVL across history (SY-based, summed protocol)
    peak = con.execute("""
        WITH daily AS (
          SELECT date, SUM(tvl_usd) AS tvl FROM main_analytics.tvl_daily GROUP BY 1
        )
        SELECT date::VARCHAR, tvl FROM daily ORDER BY tvl DESC LIMIT 1
    """).fetchone()
    # Current TVL (latest date)
    cur = con.execute("""
        WITH daily AS (
          SELECT date, SUM(tvl_usd) tvl, SUM(principal_tvl_usd) p FROM main_analytics.tvl_daily GROUP BY 1
        )
        SELECT tvl, p FROM daily ORDER BY date DESC LIMIT 1
    """).fetchone()
    # Volume aggregates
    vol = con.execute("""
        SELECT
          SUM(volume_usd)                                                            AS lifetime,
          SUM(volume_usd) FILTER (WHERE date >= CURRENT_DATE - INTERVAL 30 DAY)      AS d30
        FROM main_analytics.trading_volume_daily
    """).fetchone()
    # First activity date (earliest swap)
    first_date = con.execute(
        "SELECT MIN(date)::VARCHAR FROM main_analytics.trading_volume_daily WHERE volume_usd > 0"
    ).fetchone()[0]
    # Holders — distinct owners across PT/YT/LP on latest snapshot
    holders = con.execute("""
        SELECT COUNT(DISTINCT owner) FROM main.raw_holders
        WHERE snapshot_date = (SELECT MAX(snapshot_date) FROM main.raw_holders)
    """).fetchone()
    # PT / LP / Idle decomposition (latest)
    decomp = con.execute("""
        SELECT
          (SELECT SUM(tvl_usd)           FROM main_intermediate.int_sy_tvl_daily
            WHERE date = (SELECT MAX(date) FROM main_intermediate.int_sy_tvl_daily)) AS sy_usd,
          (SELECT SUM(principal_tvl_usd) FROM main_analytics.tvl_daily
            WHERE date = (SELECT MAX(date) FROM main_analytics.tvl_daily)) AS pt_usd,
          (SELECT SUM(usd_value)         FROM main_analytics.active_positions_daily
            WHERE leg = 'LP' AND date = (SELECT MAX(date) FROM main_analytics.active_positions_daily)) AS lp_usd
    """).fetchone()
    sy_usd = float(decomp[0] or 0); pt_usd = float(decomp[1] or 0); lp_usd = float(decomp[2] or 0)
    idle_usd = max(0.0, sy_usd - pt_usd - lp_usd)
    return {
        "meta": {"generatedAt": datetime.now(timezone.utc).isoformat()},
        "markets": {
            "active":          int(mk[0] or 0),
            "expired":         int(mk[1] or 0),
            "total":           int(mk[2] or 0),
            "platforms":       int(mk[3] or 0),
            "tickers":         int(mk[4] or 0),
            "latestMaturity":  str(mk[5]) if mk[5] else None,
        },
        "tvl": {
            "currentUsd":         float(cur[0] or 0) if cur else 0.0,
            "currentPrincipalUsd": float(cur[1] or 0) if cur else 0.0,
            "peakUsd":            float(peak[1] or 0) if peak else 0.0,
            "peakDate":           peak[0] if peak else None,
            "ptUsd":              pt_usd,
            "lpUsd":              lp_usd,
            "idleUsd":            idle_usd,
        },
        "volume": {
            "lifetimeUsd": float(vol[0] or 0) if vol else 0.0,
            "thirty30Usd": float(vol[1] or 0) if vol else 0.0,
        },
        "holders": {
            "totalUniqueOwners": int(holders[0] or 0) if holders else 0,
        },
        "protocol": {
            "firstActivityDate": first_date,
            "ageDays": (today - datetime.strptime(first_date, "%Y-%m-%d").date()).days if first_date else None,
        },
    }


def build_holders_json(con: duckdb.DuckDBPyConnection) -> dict:
    """Holder concentration per (market_key, leg). Snapshot, not timeseries.

    Shape:
      meta: {generatedAt, snapshotDate, totalHolders, mintsCovered}
      rows: [{marketKey, ticker, leg, nHolders, top1Pct, top5Pct, top10Pct, totalSupply, status, maturityDate}]
    """
    meta = con.execute("""
        SELECT MAX(snapshot_date)::VARCHAR, SUM(n_holders), COUNT(*)
        FROM main_analytics.holders_snapshot
    """).fetchone()
    rows = con.execute("""
        SELECT market_key, ticker, leg, n_holders, top1_pct, top5_pct, top10_pct,
               total_supply, status, maturity_date::VARCHAR
        FROM main_analytics.holders_snapshot
        ORDER BY n_holders DESC NULLS LAST
    """).fetchall()
    return {
        "meta": {
            "generatedAt": datetime.now(timezone.utc).isoformat(),
            "snapshotDate": meta[0],
            "totalHolders": int(meta[1] or 0),
            "mintsCovered": int(meta[2] or 0),
        },
        "rows": [
            {
                "marketKey": r[0], "ticker": r[1], "leg": r[2],
                "nHolders": int(r[3] or 0),
                "top1Pct": float(r[4] or 0), "top5Pct": float(r[5] or 0), "top10Pct": float(r[6] or 0),
                "totalSupply": float(r[7] or 0),
                "status": r[8], "maturityDate": r[9],
            }
            for r in rows
        ],
    }


def build_users_json(con: duckdb.DuckDBPyConnection) -> dict:
    """User / wallet stats: growth timeseries + headline aggregates + top wallets."""
    headline = con.execute("""
        SELECT
          COUNT(*),
          COUNT(*) FILTER (WHERE n_swaps = 1),
          COUNT(*) FILTER (WHERE n_swaps BETWEEN 2 AND 9),
          COUNT(*) FILTER (WHERE n_swaps BETWEEN 10 AND 99),
          COUNT(*) FILTER (WHERE n_swaps >= 100),
          SUM(total_volume_usd),
          AVG(active_span_days),
          MAX(n_swaps)
        FROM main_analytics.user_lifetime_stats
    """).fetchone()
    growth = con.execute("""
        SELECT date::VARCHAR, active_wallets, new_wallets, cumulative_wallets, swaps, volume_usd
        FROM main_analytics.user_growth_daily ORDER BY date
    """).fetchall()
    top = con.execute("""
        SELECT signer, n_swaps, total_volume_usd, n_markets, n_tickers,
               first_seen::VARCHAR, last_seen::VARCHAR, active_span_days,
               n_buy_yt, n_sell_yt, n_buy_pt, n_sell_pt
        FROM main_analytics.user_lifetime_stats
        ORDER BY total_volume_usd DESC NULLS LAST
        LIMIT 50
    """).fetchall()
    # 30d concentration: what % of recent volume from top N wallets
    recent = con.execute("""
        WITH recent_per_wallet AS (
          SELECT signer, SUM(notional_usd) v
          FROM main_intermediate.int_amm_swaps
          WHERE signer IS NOT NULL AND date >= CURRENT_DATE - INTERVAL 30 DAY
          GROUP BY signer
        ),
        ranked AS (
          SELECT v, ROW_NUMBER() OVER (ORDER BY v DESC) rk, SUM(v) OVER () total FROM recent_per_wallet
        )
        SELECT
          MAX(total)                                                       AS total_volume,
          SUM(v) FILTER (WHERE rk <= 10) / NULLIF(MAX(total), 0) * 100     AS top10_pct,
          SUM(v) FILTER (WHERE rk <= 100) / NULLIF(MAX(total), 0) * 100    AS top100_pct,
          COUNT(*)                                                         AS recent_wallets
        FROM ranked
    """).fetchone()
    return {
        "meta": {"generatedAt": datetime.now(timezone.utc).isoformat()},
        "headline": {
            "totalWallets":    int(headline[0] or 0),
            "oneSwap":         int(headline[1] or 0),
            "casualWallets":   int(headline[2] or 0),  # 2-9 swaps
            "activeWallets":   int(headline[3] or 0),  # 10-99
            "powerWallets":    int(headline[4] or 0),  # 100+
            "lifetimeVolumeUsd": float(headline[5] or 0),
            "avgActiveSpanDays": float(headline[6] or 0),
            "maxSwapsByOneWallet": int(headline[7] or 0),
        },
        "concentration30d": {
            "recentWallets":   int(recent[3] or 0),
            "totalVolumeUsd":  float(recent[0] or 0),
            "top10SharePct":   float(recent[1] or 0),
            "top100SharePct":  float(recent[2] or 0),
        },
        "growth": {
            "dates":             [r[0] for r in growth],
            "activeWallets":     [int(r[1] or 0) for r in growth],
            "newWallets":        [int(r[2] or 0) for r in growth],
            "cumulativeWallets": [int(r[3] or 0) for r in growth],
            "swaps":             [int(r[4] or 0) for r in growth],
            "volumeUsd":         [float(r[5] or 0) for r in growth],
        },
        "topWallets": [
            {
                "signer": r[0],
                "nSwaps": int(r[1] or 0),
                "totalVolumeUsd": float(r[2] or 0),
                "nMarkets": int(r[3] or 0),
                "nTickers": int(r[4] or 0),
                "firstSeen": r[5], "lastSeen": r[6],
                "activeSpanDays": int(r[7] or 0),
                "actions": {
                    "buyYt": int(r[8] or 0), "sellYt": int(r[9] or 0),
                    "buyPt": int(r[10] or 0), "sellPt": int(r[11] or 0),
                },
            }
            for r in top
        ],
    }


def build_market_holders_json(con: duckdb.DuckDBPyConnection) -> dict:
    """Top holders per (market_key, leg). v1-equivalent of holders.json.

    Shape: {"USX-01JUN26:PT": {holders, totalBalance, totalUsd, top: [...]}}
    """
    rows = con.execute("""
        SELECT market_key, leg, owner, amount, share_pct, usd_value, total_balance, rk,
               underlying_price_usd
        FROM main_analytics.market_holders_top
        WHERE rk <= 500
        ORDER BY market_key, leg, rk
    """).fetchall()
    out: dict[str, dict] = {}
    for mk, leg, owner, amount, share_pct, usd, total_bal, rk, price in rows:
        key = f"{mk}:{leg}"
        e = out.setdefault(key, {
            "market": mk, "leg": leg, "holders": 0,
            "totalBalance": float(total_bal or 0),
            "totalUsd": float((total_bal or 0) * (price or 0)),
            "top": [],
        })
        e["holders"] += 1
        e["top"].append({
            "owner": owner,
            "balance": float(amount or 0),
            "usd": float(usd or 0),
            "sharePct": float(share_pct or 0),
        })
    # nHolders from holders_snapshot (full count, not just top 500)
    counts = dict(con.execute("""
        SELECT market_key || ':' || leg, n_holders FROM main_analytics.holders_snapshot
    """).fetchall())
    for k, v in out.items():
        v["holders"] = int(counts.get(k, v["holders"]))
    return {
        "meta": {"generatedAt": datetime.now(timezone.utc).isoformat(), "markets": len(out)},
        "byMarketLeg": out,
    }


def build_unclaimed_yield_json(con: duckdb.DuckDBPyConnection) -> dict:
    """Wallets with unclaimed YT (holds YT but never claimYield'd that market)."""
    rows = con.execute("""
        SELECT wallet, market_key, yt_balance FROM main_analytics.unclaimed_yield
    """).fetchall()
    by_wallet: dict[str, list[dict]] = {}
    for wallet, mk, bal in rows:
        by_wallet.setdefault(wallet, []).append(
            {"marketKey": mk, "ytBalance": float(bal or 0)}
        )
    return {
        "meta": {"generatedAt": datetime.now(timezone.utc).isoformat(),
                 "totalPositions": len(rows), "totalWallets": len(by_wallet)},
        "byWallet": by_wallet,
    }


def build_wallet_shards(con: duckdb.DuckDBPyConnection) -> None:
    """Emit one JSON per wallet under web/public/wallet/{addr}.json.

    Filters to wallets with >= 3 events to keep file count reasonable
    (~20K files for ~50K total wallets indexed). Each shard includes the
    full event log with per-tx signer-side token deltas, symbol-resolved.
    """
    shard_dir = WEB_PUBLIC / "wallet"
    if shard_dir.exists():
        for f in shard_dir.glob("*.json"):
            f.unlink()
    shard_dir.mkdir(parents=True, exist_ok=True)

    rprint("  fetching per-event token changes…")
    # Build a giant cursor: for each (wallet, sig) emit its events + the wallet's own
    # token deltas, joined to token metadata for symbol.
    rows = con.execute("""
        WITH eligible_wallets AS (
            SELECT signer FROM main_analytics.wallet_events
            WHERE signer IS NOT NULL
            GROUP BY signer
            HAVING COUNT(*) >= 3
        ),
        ev AS (
            SELECT e.signer, e.signature, e.block_time, e.action,
                   e.market_key, e.ticker, e.usd_value
            FROM main_analytics.wallet_events e
            JOIN eligible_wallets ew USING (signer)
        ),
        changes AS (
            SELECT c.signature, c.owner,
                   c.mint, c.delta_ui,
                   COALESCE(tm.symbol, et.symbol, SUBSTR(c.mint, 1, 4) || '…') AS symbol,
                   c.delta_ui * p.price_usd AS usd_delta
            FROM main_staging.stg_token_changes c
            LEFT JOIN main.raw_token_metadata tm ON tm.mint = c.mint
            LEFT JOIN main.raw_exponent_tokens et ON et.mint = c.mint
            LEFT JOIN main_staging.stg_prices p
                ON p.mint = c.mint AND p.date = TO_TIMESTAMP(c.block_time)::DATE
        )
        SELECT
            ev.signer, ev.signature, ev.block_time, ev.action,
            ev.market_key, ev.ticker, ev.usd_value,
            LIST({
              'symbol': ch.symbol,
              'delta':  ch.delta_ui,
              'usd':    ch.usd_delta
            } ORDER BY ABS(ch.delta_ui) DESC) FILTER (WHERE ch.symbol IS NOT NULL) AS changes
        FROM ev
        LEFT JOIN changes ch
          ON ch.signature = ev.signature AND ch.owner = ev.signer
        GROUP BY 1, 2, 3, 4, 5, 6, 7
        ORDER BY ev.signer, ev.block_time DESC
    """).fetchall()

    rprint(f"  grouping {len(rows):,} events by wallet…")
    by_wallet: dict[str, list[dict]] = {}
    for signer, sig, bt, action, mk, ticker, usd, changes in rows:
        by_wallet.setdefault(signer, []).append({
            "sig": sig,
            "blockTime": int(bt or 0),
            "action": action,
            "market": mk,
            "ticker": ticker,
            "usd": float(usd or 0) if usd is not None else None,
            "changes": [
                {"symbol": c["symbol"], "delta": float(c["delta"] or 0),
                 "usd": float(c["usd"] or 0) if c["usd"] is not None else None}
                for c in (changes or [])
            ],
        })

    rprint(f"  writing {len(by_wallet):,} wallet shards…")
    for wallet, events in by_wallet.items():
        with open(shard_dir / f"{wallet}.json", "w") as f:
            json.dump({"wallet": wallet, "events": events}, f, separators=(",", ":"))
    rprint(f"[green]wrote {len(by_wallet):,} wallet shards[/green] ({sum(len(e) for e in by_wallet.values()):,} events total)")


def build() -> None:
    WEB_PUBLIC.mkdir(parents=True, exist_ok=True)
    con = duckdb.connect(str(WAREHOUSE_PATH), read_only=True)
    try:
        for name, builder in [
            ("stats.json", build_stats_json),
            ("volume.json", build_volume_json),
            ("tvl.json", build_tvl_json),
            ("active_positions.json", build_active_positions_json),
            ("holders.json", build_holders_json),
            ("market_holders.json", build_market_holders_json),
            ("market_share.json", build_market_share_json),
            ("users.json", build_users_json),
            ("unclaimed_yield.json", build_unclaimed_yield_json),
        ]:
            rprint(f"[cyan]Building {name}…[/cyan]")
            payload = builder(con)
            _write_atomic(WEB_PUBLIC / name, payload)
            size = (WEB_PUBLIC / name).stat().st_size / 1024
            rprint(f"  wrote {name}  ({size:.1f} KB)")
        rprint("[cyan]Building wallet shards…[/cyan]")
        build_wallet_shards(con)
    finally:
        con.close()
    rprint("[green]serve done[/green]")


if __name__ == "__main__":
    build()
