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
import math
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
    "USD*": "Perena",
    "MLP": "MarginFi", "ALP": "Adrena",
    # syUSDC / syrupUSDC = Maple Finance's Syrup USDC (NOT Solend — early
    # 'syUSDC' was Exponent's short form for syrupUSDC; underlying mint
    # tzqPfHkN… is labeled "Exponent Wrapped syrupUSDC" in Metaplex).
    "syUSDC": "Maple", "syrupUSDC": "Maple",
    # Per-protocol USDC variants — the prefix indicates the issuer.
    "USDC+":   "Reflect",   # Reflect Protocol's stablecoin vault
    "mUSDC":   "Marinade",  # Marinade's USDC product
    "kUSDC":   "Kyros",     # Kyros's USDC vault
    "wkySOL":  "Kyros",     # wrapped kySOL
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

    # Per-market series — split USD volume into 4 buckets so the market
    # detail page can render the buy/sell decomposition:
    #   ptBuyUsd, ptSellUsd, ytBuyUsd, ytSellUsd. Sum = totalUsd.
    per_market_rows = con.execute(
        """
        SELECT market_key, ticker, date::VARCHAR, side,
               SUM(volume_usd_buys)  AS buy_usd,
               SUM(volume_usd_sells) AS sell_usd,
               SUM(volume_underlying) AS v_und
        FROM main_analytics.trading_volume_daily
        GROUP BY 1, 2, 3, 4
        """
    ).fetchall()
    by_market: dict[str, dict] = {}
    for market_key, ticker, date, side, buy_usd, sell_usd, v_und in per_market_rows:
        entry = by_market.setdefault(
            market_key,
            {
                "ticker": ticker,
                "pt_buy_map": {}, "pt_sell_map": {},
                "yt_buy_map": {}, "yt_sell_map": {},
                "pt_und_map": {}, "yt_und_map": {},
            },
        )
        if side == "PT":
            entry["pt_buy_map"][date]  = float(buy_usd or 0)
            entry["pt_sell_map"][date] = float(sell_usd or 0)
            entry["pt_und_map"][date]  = float(v_und or 0)
        elif side == "YT":
            entry["yt_buy_map"][date]  = float(buy_usd or 0)
            entry["yt_sell_map"][date] = float(sell_usd or 0)
            entry["yt_und_map"][date]  = float(v_und or 0)

    by_market_out: dict[str, dict] = {}
    market_totals: list[tuple[str, str, float]] = []
    for market_key, entry in by_market.items():
        pt_buy   = [entry["pt_buy_map"].get(d, 0.0)  for d in dates]
        pt_sell  = [entry["pt_sell_map"].get(d, 0.0) for d in dates]
        yt_buy   = [entry["yt_buy_map"].get(d, 0.0)  for d in dates]
        yt_sell  = [entry["yt_sell_map"].get(d, 0.0) for d in dates]
        pt_usd_arr = [a + b for a, b in zip(pt_buy, pt_sell)]
        yt_usd_arr = [a + b for a, b in zip(yt_buy, yt_sell)]
        pt_und_arr = [entry["pt_und_map"].get(d, 0.0) for d in dates]
        yt_und_arr = [entry["yt_und_map"].get(d, 0.0) for d in dates]
        total_usd_arr = [a + b for a, b in zip(pt_usd_arr, yt_usd_arr)]
        total_und_arr = [a + b for a, b in zip(pt_und_arr, yt_und_arr)]
        total_usd_sum = sum(total_usd_arr)
        by_market_out[market_key] = {
            "ticker": entry["ticker"],
            "ptUsd": pt_usd_arr,
            "ytUsd": yt_usd_arr,
            "ptBuyUsd":  pt_buy,
            "ptSellUsd": pt_sell,
            "ytBuyUsd":  yt_buy,
            "ytSellUsd": yt_sell,
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
    # Date range: take the wider of (tvl_daily, int_sy_tvl_daily). The
    # protocol total comes from int_sy_tvl_daily which carries SY-only
    # capital from before any market was split into PT/YT.
    bounds = con.execute("""
        SELECT
          LEAST((SELECT MIN(date) FROM main_intermediate.int_sy_tvl_daily),
                (SELECT MIN(date) FROM main_analytics.tvl_daily))::VARCHAR,
          GREATEST((SELECT MAX(date) FROM main_intermediate.int_sy_tvl_daily),
                   (SELECT MAX(date) FROM main_analytics.tvl_daily))::VARCHAR
    """).fetchone()
    if not bounds or not bounds[0]:
        return {"meta": {"generatedAt": datetime.now(timezone.utc).isoformat(), "empty": True}}
    min_d, max_d = bounds
    dates = [r[0] for r in con.execute(
        f"SELECT generate_series::DATE::VARCHAR FROM generate_series(DATE '{min_d}', DATE '{max_d}', INTERVAL 1 DAY)"
    ).fetchall()]
    # Protocol total: sum at sy_mint level (covers SY held raw before any
    # market is split). Principal = PT-only, from tvl_daily.
    proto_sy = con.execute("""
        SELECT date::VARCHAR, SUM(tvl_usd)
        FROM main_intermediate.int_sy_tvl_daily GROUP BY 1
    """).fetchall()
    proto_pt = con.execute("""
        SELECT date::VARCHAR, SUM(principal_tvl_usd)
        FROM main_analytics.tvl_daily GROUP BY 1
    """).fetchall()
    proto_sy_map = {r[0]: float(r[1] or 0) for r in proto_sy}
    proto_pt_map = {r[0]: float(r[1] or 0) for r in proto_pt}
    proto_usd       = [proto_sy_map.get(d, 0.0) for d in dates]
    proto_principal = [proto_pt_map.get(d, 0.0) for d in dates]
    # Per-market series — load raw platform + PT/YT split too so we can roll
    # up byPlatform AND emit per-market breakdown buckets
    rows = con.execute("""
        SELECT t.market_key, t.ticker, t.platform, t.date::VARCHAR,
               t.tvl_usd, t.tvl_underlying, t.principal_tvl_usd,
               t.principal_pt_usd, t.farm_yt_usd
        FROM main_analytics.tvl_daily t
    """).fetchall()
    # Test markets — propagate dim_markets.is_test so the web can label
    # known on-chain-but-never-productized markets explicitly.
    test_markets = {
        mk for (mk,) in con.execute(
            "SELECT market_key FROM main_core.dim_markets WHERE is_test"
        ).fetchall()
    }
    # Liquidity — exact replica of Exponent's UI value, fully on-chain.
    # Formula from the SDK (market.js): sy_balance × sy_rate + pt_balance /
    # exp(last_ln_implied × years_remaining). sy_balance, pt_balance, and
    # last_ln_implied_rate are read from MarketTwo.financials (offsets 380,
    # 372, 396) in the on-chain account itself — same values the SDK reads.
    # USD price comes from Jupiter/Pyth via stg_prices.
    # Verified against API legacyLiquidity for USX-01JUN26: derived
    # 26,113,411 USX vs API 26,113,405 USX (4-unit slot drift).
    # Several markets have multiple pool_accounts (deprecated v1 pool + live v2
    # pool sharing the same market_key). Sum across pools so the LP column
    # reflects total pool reserves, not just whichever pool was last seen.
    liquidity_by_market: dict[str, float] = {
        mk: float(v or 0) for mk, v in con.execute(
            """SELECT market_key, SUM(liquidity_usd)
               FROM main_intermediate.int_pool_reserves_daily
               GROUP BY market_key"""
        ).fetchall()
    }
    # Canonical PT price ratio per market — SDK time-curve, not AMM-median.
    # int_implied_prices_daily uses the median of per-trade implied prices,
    # which is noise-dominated for low-volume markets (~6 trades/day on
    # ONyc-10SEP26 produced ratio=0.87 vs the SDK curve's 0.97, blowing up
    # implied yield to 51% instead of ~13%). This canonical ratio comes
    # straight from pt_exchange_rate = exp(last_ln_implied × years_remaining),
    # the same value Exponent's SDK and our int_wallet_holdings_usd already
    # use. Pick the largest pool by liquidity_usd when a market has multiple
    # pools (deprecated v1 + live v2).
    pt_ratio_by_market: dict[str, float] = {
        mk: float(v or 0) for mk, v in con.execute(
            """WITH ranked AS (
                 SELECT market_key,
                        1.0 / NULLIF(pt_exchange_rate, 0) AS pt_ratio,
                        last_ln_implied_rate,
                        liquidity_usd,
                        ROW_NUMBER() OVER (PARTITION BY market_key
                                           ORDER BY liquidity_usd DESC) AS rn
                 FROM main_intermediate.int_pool_reserves_daily
                 WHERE market_key IS NOT NULL
               )
               SELECT market_key, pt_ratio FROM ranked WHERE rn = 1"""
        ).fetchall()
    }
    # Implied APY — annualized convention to match Exponent's UI display.
    # last_ln_implied_rate is the continuously-compounded rate from the SDK
    # time-curve (pt_exchange_rate = exp(r × t) where r = last_ln_implied_rate).
    # Convert to annualized APY: APY = exp(r) − 1. The underlying rate is
    # still 100% from our on-chain MarketTwo decode (offset 396); the math
    # just matches the convention Exponent's "Implied APY" uses.
    # Continuous 12.73% → annualized 13.57% (≈ Exponent's 13.56% for ONyc-10SEP26).
    implied_yield_by_market: dict[str, float] = {
        mk: float(v or 0) for mk, v in con.execute(
            """WITH ranked AS (
                 SELECT market_key, last_ln_implied_rate, liquidity_usd,
                        ROW_NUMBER() OVER (PARTITION BY market_key
                                           ORDER BY liquidity_usd DESC) AS rn
                 FROM main_intermediate.int_pool_reserves_daily
                 WHERE market_key IS NOT NULL
               )
               SELECT market_key, exp(last_ln_implied_rate) - 1.0 FROM ranked WHERE rn = 1"""
        ).fetchall()
    }
    # Implied APY ~7 days ago — picks the raw_market_two_pools snapshot
    # whose age sits closest to 7 days within a [4, 14] day window
    # (avoids leaning on a stale outlier if the only history is very old).
    # Returns (value, snapshot_date) per market. Returns None when no
    # snapshot falls in range. Page formats delta + actual day count.
    implied_yield_past_by_market: dict[str, tuple[float, str]] = {}
    for mk, llir, snap_date in con.execute(
        """WITH per_pool AS (
             SELECT m.market_key, p.snapshot_date, p.last_ln_implied_rate,
                    p.pt_balance_raw,
                    ROW_NUMBER() OVER (PARTITION BY m.market_key, p.snapshot_date
                                       ORDER BY p.pt_balance_raw DESC) AS rn
             FROM main.raw_market_two_pools p
             JOIN main_core.dim_markets m ON m.pt_mint = p.mint_pt
           ),
           with_age AS (
             SELECT market_key, snapshot_date, last_ln_implied_rate,
                    DATE_DIFF('day', snapshot_date, CURRENT_DATE) AS age_days,
                    ABS(DATE_DIFF('day', snapshot_date, CURRENT_DATE) - 7) AS distance_from_7d
             FROM per_pool WHERE rn = 1
           ),
           ranked_window AS (
             SELECT market_key, snapshot_date, last_ln_implied_rate, age_days,
                    ROW_NUMBER() OVER (PARTITION BY market_key
                                       ORDER BY distance_from_7d ASC, age_days ASC) AS rn
             FROM with_age WHERE age_days BETWEEN 4 AND 14
           )
           SELECT market_key, last_ln_implied_rate, snapshot_date::VARCHAR
           FROM ranked_window WHERE rn = 1"""
    ).fetchall():
        implied_yield_past_by_market[mk] = (math.exp(float(llir)) - 1.0, snap_date)
    # Daily implied-yield HISTORY per (market, date) — from the trade-derived
    # int_implied_prices_daily pt_price_ratio, annualized by days-to-maturity
    # at each date: IY = (1/pt_ratio)^(365/days_remaining) − 1. This is the
    # market's traded implied APY per day (gaps on no-trade days). Distinct
    # from the scalar `impliedYield` above, which is today's SDK-curve value.
    iy_series_by_market: dict[str, dict[str, float]] = {}
    for mk, d, iy in con.execute(
        """SELECT ip.market_key, ip.date::VARCHAR,
                  pow(1.0 / ip.pt_price_ratio,
                      365.0 / GREATEST(DATE_DIFF('day', ip.date, m.maturity_date), 1)) - 1.0
           FROM main_intermediate.int_implied_prices_daily ip
           JOIN main_core.dim_markets m USING (market_key)
           WHERE ip.pt_price_ratio > 0 AND ip.pt_price_ratio < 1
             AND m.maturity_date IS NOT NULL
             AND ip.date < m.maturity_date"""
    ).fetchall():
        iy_series_by_market.setdefault(mk, {})[d] = float(iy)
    # LP USD per (market, date) — keyed for fast lookup in per-market breakdown
    lp_per_market = {(r[0], r[1]): float(r[2] or 0) for r in con.execute("""
        SELECT market_key, date::VARCHAR, SUM(usd_value)
        FROM main_analytics.active_positions_daily
        WHERE leg = 'LP' AND market_key IS NOT NULL
        GROUP BY 1, 2
    """).fetchall()}
    by_market: dict[str, dict] = {}
    platform_for_mk: dict[str, str] = {}
    for mk, ticker, raw_plat, date, tvl_usd, tvl_und, principal, pt_v, yt_v in rows:
        e = by_market.setdefault(mk, {
            "ticker": ticker, "platform": normalize_platform(raw_plat, ticker),
            "usd_map": {}, "und_map": {}, "principal_map": {},
            "pt_map": {}, "yt_map": {},
        })
        platform_for_mk[mk] = e["platform"]
        e["usd_map"][date] = float(tvl_usd or 0)
        e["und_map"][date] = float(tvl_und or 0)
        e["principal_map"][date] = float(principal or 0)
        e["pt_map"][date] = float(pt_v or 0)
        e["yt_map"][date] = float(yt_v or 0)
    by_market_out: dict[str, dict] = {}
    totals = []
    for mk, e in by_market.items():
        usd = [e["usd_map"].get(d, 0.0) for d in dates]
        und = [e["und_map"].get(d, 0.0) for d in dates]
        principal = [e["principal_map"].get(d, 0.0) for d in dates]
        pt_raw_m = [e["pt_map"].get(d, 0.0) for d in dates]
        yt_raw_m = [e["yt_map"].get(d, 0.0) for d in dates]
        pt_arr_m: list[float] = []
        yt_arr_m: list[float] = []
        lp_arr_m: list[float] = []
        idle_arr_m: list[float] = []
        # LP uses pool-reserves USD (= "Liquidity"), not active_positions_daily.
        # The latter has spotty coverage and gets squeezed to $0 by the
        # scope-budget cap whenever PT face saturates scope (USX-16SEP26 case).
        # Pool reserves are derivable on-chain for every market with an AMM,
        # so we get a true LP figure even when face-value buckets overlap it.
        lp_pool_usd = liquidity_by_market.get(mk, 0.0)
        for i, d in enumerate(dates):
            scope_tvl = usd[i]
            pt_raw = pt_raw_m[i]
            yt_raw = yt_raw_m[i]
            # If PT+YT face exceeds scope_tvl (flash-minted pool PT inflates
            # the face count), scale them so the principal columns stay
            # interpretable. LP and Idle are reported independently.
            ptyt_total = pt_raw + yt_raw
            if ptyt_total > scope_tvl and ptyt_total > 0:
                scale = scope_tvl / ptyt_total
                pt_v = pt_raw * scale
                yt_v = yt_raw * scale
            else:
                pt_v = pt_raw
                yt_v = yt_raw
            # LP = current pool reserves (constant across dates — we don't
            # have a daily pool-reserves history yet, only "latest"). Use
            # today's value as a single point at the latest date so the
            # chart doesn't draw a phantom line.
            lp_v = lp_pool_usd if i == len(dates) - 1 else 0.0
            # Idle = vault SY allocation that's neither in user PT/YT nor in
            # the pool. Can be 0 for healthy markets where the pool absorbs
            # everything; positive when the vault has unsplit/unparked SY.
            idle_v = max(0.0, scope_tvl - pt_v - yt_v - lp_v)
            pt_arr_m.append(pt_v)
            yt_arr_m.append(yt_v)
            lp_arr_m.append(lp_v)
            idle_arr_m.append(idle_v)
        by_market_out[mk] = {
            "ticker": e["ticker"],
            "platform": e["platform"],
            "isTest": mk in test_markets,
            # Liquidity (= Exponent UI's Liquidity) — fully derived on-chain
            # via the SDK formula. See liquidity_by_market construction above.
            "liquidityUsd": liquidity_by_market.get(mk, 0.0),
            # Active TVL = principalUsd[latest], derived fully from on-chain
            # PT supply × Jupiter/Pyth USD price.
            "activeTvlUsd": principal[-1] if principal else 0.0,
            # Canonical PT price ratio + implied yield from the SDK time-curve
            # (NOT the noisy AMM-median that feeds ptUsd/ytUsd arrays). Use these
            # for the latest-day hero/position values; the historical arrays are
            # kept as-is for the chart timeseries.
            "ptRatio":      pt_ratio_by_market.get(mk),
            "impliedYield": implied_yield_by_market.get(mk),
            # Past implied yield for 7d delta on the hero number. We pick the
            # snapshot whose age sits closest to 7 days within [4, 14] days;
            # the actual date is emitted so the page labels honestly (e.g.
            # "Past 12 Days" while history is sparse during early Railway runs).
            "impliedYield7dAgo":     (implied_yield_past_by_market.get(mk) or (None, None))[0],
            "impliedYield7dAgoDate": (implied_yield_past_by_market.get(mk) or (None, None))[1],
            # Daily traded implied-yield series (aligned to `dates`; null on
            # no-trade days). Powers the Implied Yield chart on the market page.
            "impliedYieldSeries": [iy_series_by_market.get(mk, {}).get(d) for d in dates],
            "tvlUsd": usd,
            "tvlUnderlying": und,
            "principalUsd": principal,
            # 4-bucket breakdown (sums to tvlUsd)
            "ptUsd":    pt_arr_m,
            "ytUsd":    yt_arr_m,
            "lpUsd":    lp_arr_m,
            "idleUsd":  idle_arr_m,
        }
        totals.append((mk, e["ticker"] or "", usd[-1] if usd else 0))
    totals.sort(key=lambda x: -x[2])
    top = [{"marketKey": mk, "ticker": tk, "tvlUsdNow": v} for mk, tk, v in totals[:20]]

    # Per-platform daily timeseries — sum int_sy_tvl_daily (mint level) by
    # platform. We CANNOT sum per-market tvl values here: per-market TVL
    # only exists once a market has PT/YT (post-split), so summing them
    # would zero out the SY-only history and create a fake step-up the day
    # the first market in a platform splits its SY. Going to the sy_mint
    # level captures all SY ever deposited, including the raw/LP-held
    # portion that has no per-market identity.
    sy_plat_rows = con.execute("""
        WITH mint_to_platform AS (
          SELECT sy_mint,
                 ANY_VALUE(ticker)   AS ticker,
                 ANY_VALUE(platform) AS platform
          FROM main_core.dim_markets
          WHERE sy_mint IS NOT NULL
          GROUP BY sy_mint
        )
        SELECT s.date::VARCHAR, mtp.platform, mtp.ticker, SUM(s.tvl_usd) AS tvl
        FROM main_intermediate.int_sy_tvl_daily s
        LEFT JOIN mint_to_platform mtp USING (sy_mint)
        GROUP BY 1, 2, 3
    """).fetchall()
    by_platform: dict[str, list[float]] = {}
    for date, raw_plat, ticker, tvl in sy_plat_rows:
        plat = normalize_platform(raw_plat, ticker)
        if plat not in by_platform:
            by_platform[plat] = [0.0] * len(dates)
        try:
            idx = dates.index(date)
            by_platform[plat][idx] += float(tvl or 0)
        except ValueError:
            pass
    platforms_sorted = sorted(
        by_platform.items(),
        key=lambda kv: (-kv[1][-1] if kv[1] else 0, kv[0])
    )

    # Per-platform 4-bucket breakdown: sum PT/YT per platform (from
    # per-market data) + LP per platform + Idle = platform_total - PT - YT - LP.
    # Buckets sum to the platform total at every date.
    platform_pt: dict[str, list[float]] = {}
    platform_yt: dict[str, list[float]] = {}
    platform_lp: dict[str, list[float]] = {}
    for mk, m in by_market_out.items():
        plat = m.get("platform") or "Other"
        if plat not in platform_pt:
            platform_pt[plat] = [0.0] * len(dates)
            platform_yt[plat] = [0.0] * len(dates)
            platform_lp[plat] = [0.0] * len(dates)
        for i in range(len(dates)):
            platform_pt[plat][i] += m["ptUsd"][i]
            platform_yt[plat][i] += m["ytUsd"][i]
            platform_lp[plat][i] += m["lpUsd"][i]
    by_platform_breakdown: dict[str, dict[str, list[float]]] = {}
    for plat, plat_series in by_platform.items():
        pt_p = platform_pt.get(plat, [0.0] * len(dates))
        yt_p = platform_yt.get(plat, [0.0] * len(dates))
        lp_p = platform_lp.get(plat, [0.0] * len(dates))
        idle_p: list[float] = []
        lp_capped: list[float] = []
        for i in range(len(dates)):
            scope = plat_series[i]
            remaining = max(0.0, scope - pt_p[i] - yt_p[i])
            lp_cap_v = min(lp_p[i], remaining)
            lp_capped.append(lp_cap_v)
            idle_p.append(max(0.0, remaining - lp_cap_v))
        by_platform_breakdown[plat] = {
            "principalPt": pt_p,
            "farmYt":      yt_p,
            "liquidityLp": lp_capped,
            "idle":        idle_p,
        }

    # Decomposition: PT + YT + LP + Idle = SY total per day.
    # PT/YT now valued at AMM-implied market prices (not face value), so YT
    # gets its own visible bucket. v1's "Income / Farm / Liquidity / Idle".
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
        pt_yt AS (
          SELECT date, SUM(principal_pt_usd) AS pt_usd, SUM(farm_yt_usd) AS yt_usd
          FROM main_analytics.tvl_daily GROUP BY date
        )
        SELECT sy.date::VARCHAR, sy.sy_usd,
               COALESCE(pt_yt.pt_usd, 0), COALESCE(pt_yt.yt_usd, 0),
               COALESCE(lp.lp_usd, 0)
        FROM sy LEFT JOIN pt_yt USING (date) LEFT JOIN lp USING (date)
    """).fetchall()
    # LP face-value over-counts pool's PT-side (which is already in PT_supply).
    # Cap LP at TVL - PT - YT so the four buckets add to TVL. Idle = residual.
    decomp_map = {r[0]: (float(r[1] or 0), float(r[2] or 0), float(r[3] or 0), float(r[4] or 0)) for r in decomp}
    pt_arr, yt_arr, lp_arr, idle_arr = [], [], [], []
    for d in dates:
        sy_total, pt_v, yt_v, lp_face = decomp_map.get(d, (0,0,0,0))
        remaining = max(0.0, sy_total - pt_v - yt_v)
        lp_capped = min(lp_face, remaining)
        idle_v = max(0.0, remaining - lp_capped)
        pt_arr.append(pt_v); yt_arr.append(yt_v); lp_arr.append(lp_capped); idle_arr.append(idle_v)

    # Per-market view CAN'T be summed from tvl_daily alone — that excludes
    # unsplit SY (held raw, or LP'd) which has no per-market identity. For
    # 2025 the missing share is 50–86% of protocol total. To make the stack
    # add up to the protocol total, emit one synthetic "unsplit" market per
    # platform with the residual SY value.
    attributed_per_plat_day: dict[tuple[str, int], float] = {}
    for mk, m in by_market_out.items():
        plat = m["platform"]
        for i, v in enumerate(m["tvlUsd"]):
            attributed_per_plat_day[(plat, i)] = attributed_per_plat_day.get((plat, i), 0.0) + v
    for plat, plat_series in by_platform.items():
        unsplit = [
            max(0.0, plat_series[i] - attributed_per_plat_day.get((plat, i), 0.0))
            for i in range(len(dates))
        ]
        if sum(unsplit) > 0:
            synth_key = f"{plat} (unsplit)"
            by_market_out[synth_key] = {
                "ticker": plat,
                "platform": plat,
                "synthetic": True,
                "tvlUsd": unsplit,
                "tvlUnderlying": [0.0] * len(dates),
                "principalUsd": [0.0] * len(dates),
            }

    # TGE markers: curated list of actual token-launch events for platforms
    # whose assets exist on Exponent. Lives in data/tge_dates.json so dates
    # can be edited without touching code. Only emit markers within the
    # chart's date range.
    tge_markers: list[dict] = []
    tge_file = ROOT / "data" / "tge_dates.json"
    if tge_file.exists():
        try:
            tge_data = json.loads(tge_file.read_text())
            for t in tge_data.get("tges", []):
                d = t.get("date")
                if not d or d < min_d or d > max_d:
                    continue
                tge_markers.append({
                    "platform": t.get("platform", "?"),
                    "token":    t.get("token"),
                    "date":     d,
                })
            tge_markers.sort(key=lambda m: m["date"])
        except Exception as e:
            rprint(f"[yellow]warn[/yellow] failed to load tge_dates.json: {e}")

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
            "farmYt":      yt_arr,
            "liquidityLp": lp_arr,
            "idle":        idle_arr,
        },
        "byPlatform": {plat: series for plat, series in platforms_sorted},
        "byPlatformBreakdown": by_platform_breakdown,
        "byMarket": by_market_out,
        "topMarkets": top,
        "tgeMarkers": tge_markers,
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
    # Holders — distinct owners across PT/YT/LP. int_holders_current unions
    # PT (from raw_holders snapshot) with YT/LP (from raw_positions Anchor
    # accounts), so this count reflects real user wallets, not pool PDAs.
    holders = con.execute("""
        SELECT COUNT(DISTINCT owner) FROM main_intermediate.int_holders_current
    """).fetchone()
    # PT / YT / LP / Idle decomposition (latest) — market-priced
    decomp = con.execute("""
        SELECT
          (SELECT SUM(tvl_usd)           FROM main_intermediate.int_sy_tvl_daily
            WHERE date = (SELECT MAX(date) FROM main_intermediate.int_sy_tvl_daily)) AS sy_usd,
          (SELECT SUM(principal_pt_usd)  FROM main_analytics.tvl_daily
            WHERE date = (SELECT MAX(date) FROM main_analytics.tvl_daily)) AS pt_usd,
          (SELECT SUM(farm_yt_usd)       FROM main_analytics.tvl_daily
            WHERE date = (SELECT MAX(date) FROM main_analytics.tvl_daily)) AS yt_usd,
          (SELECT SUM(usd_value)         FROM main_analytics.active_positions_daily
            WHERE leg = 'LP' AND date = (SELECT MAX(date) FROM main_analytics.active_positions_daily)) AS lp_usd
    """).fetchone()
    sy_usd = float(decomp[0] or 0)
    pt_usd = float(decomp[1] or 0)
    yt_usd = float(decomp[2] or 0)
    lp_face = float(decomp[3] or 0)
    # Cap LP at the SY remaining after PT+YT (LP face over-counts PT-in-pool)
    remaining = max(0.0, sy_usd - pt_usd - yt_usd)
    lp_usd = min(lp_face, remaining)
    idle_usd = max(0.0, remaining - lp_usd)
    # Week-ago snapshot for the same fields. Use the latest date minus 7
    # days, falling back to whatever's closest if 7d ago has no row.
    week_ago = con.execute("""
        WITH target AS (
          SELECT (SELECT MAX(date) FROM main_intermediate.int_sy_tvl_daily) - INTERVAL 7 DAY AS d
        ),
        sy_w AS (
          SELECT SUM(tvl_usd) v FROM main_intermediate.int_sy_tvl_daily
          WHERE date = (SELECT d::DATE FROM target)
        ),
        pt_w AS (
          SELECT SUM(principal_pt_usd) v FROM main_analytics.tvl_daily
          WHERE date = (SELECT d::DATE FROM target)
        ),
        yt_w AS (
          SELECT SUM(farm_yt_usd) v FROM main_analytics.tvl_daily
          WHERE date = (SELECT d::DATE FROM target)
        ),
        lp_w AS (
          SELECT SUM(usd_value) v FROM main_analytics.active_positions_daily
          WHERE leg = 'LP' AND date = (SELECT d::DATE FROM target)
        ),
        mkt_w AS (
          SELECT COUNT(*) FILTER (WHERE maturity_date >= (SELECT d::DATE FROM target)) AS active
          FROM main_core.dim_markets WHERE maturity_date IS NOT NULL
        )
        SELECT (SELECT v FROM sy_w), (SELECT v FROM pt_w), (SELECT v FROM yt_w),
               (SELECT v FROM lp_w), (SELECT active FROM mkt_w)
    """).fetchone()
    sy_w    = float(week_ago[0] or 0); pt_w  = float(week_ago[1] or 0)
    yt_w    = float(week_ago[2] or 0); lp_wf = float(week_ago[3] or 0)
    rem_w   = max(0.0, sy_w - pt_w - yt_w)
    lp_w    = min(lp_wf, rem_w)
    idle_w  = max(0.0, rem_w - lp_w)
    active_w = int(week_ago[4] or 0)
    # Volume in last 7 days (the "weekly delta" for All-Time Volume = additions this week)
    vol7 = con.execute("""
        SELECT COALESCE(SUM(volume_usd), 0)
        FROM main_analytics.trading_volume_daily
        WHERE date > (SELECT MAX(date) FROM main_analytics.trading_volume_daily) - INTERVAL 7 DAY
    """).fetchone()[0] or 0
    # Holders one week ago (closest snapshot ≤ today-7d)
    holders_w = con.execute("""
        SELECT COUNT(DISTINCT owner) FROM main.raw_holders
        WHERE snapshot_date = (
          SELECT MAX(snapshot_date) FROM main.raw_holders
          WHERE snapshot_date <= (SELECT MAX(snapshot_date) FROM main.raw_holders) - INTERVAL 7 DAY
        )
    """).fetchone()
    holders_w_n = int(holders_w[0] or 0) if holders_w else 0
    # New holders in the last 7d (proxy: wallets whose first_seen is in
    # the last 7 days, from user_growth_daily). Falls back to 0 if mart
    # not built yet.
    try:
        new_holders_7d_row = con.execute("""
            SELECT COALESCE(SUM(new_wallets), 0)
            FROM main_analytics.user_growth_daily
            WHERE date > (SELECT MAX(date) FROM main_analytics.user_growth_daily) - INTERVAL 7 DAY
        """).fetchone()
        new_holders_7d = int(new_holders_7d_row[0] or 0) if new_holders_7d_row else 0
    except Exception:
        new_holders_7d = 0
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
            "ytUsd":              yt_usd,
            "lpUsd":              lp_usd,
            "idleUsd":            idle_usd,
            "weekAgo": {
                "totalUsd": sy_w, "ptUsd": pt_w, "ytUsd": yt_w, "lpUsd": lp_w, "idleUsd": idle_w,
            },
        },
        "volume": {
            "lifetimeUsd": float(vol[0] or 0) if vol else 0.0,
            "thirty30Usd": float(vol[1] or 0) if vol else 0.0,
            "sevenDayUsd": float(vol7),
        },
        "holders": {
            "totalUniqueOwners": int(holders[0] or 0) if holders else 0,
            "weekAgo": holders_w_n,
            "newSevenDay": new_holders_7d,
        },
        "marketsActiveWeekAgo": active_w,
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
        SELECT h.market_key, h.ticker, h.leg, h.n_holders, h.top1_pct, h.top5_pct, h.top10_pct,
               h.total_supply, h.status, h.maturity_date::VARCHAR,
               coalesce(m.is_test, false) AS is_test
        FROM main_analytics.holders_snapshot h
        LEFT JOIN main_core.dim_markets m ON m.market_key = h.market_key
        ORDER BY h.n_holders DESC NULLS LAST
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
                "isTest": bool(r[10]),
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
    # Holders over time per leg (PT/YT/LP). Backfilled from tx history; see
    # int_pt_holders_daily + int_yt_lp_holders_daily for caveats. `source`
    # is 'snapshot' on dates we have a raw_holders/raw_positions snapshot,
    # else 'reconstructed' from the tx-history derivation.
    try:
        h_rows = con.execute("""
            SELECT date::VARCHAR, leg, n_holders, source
            FROM main_analytics.holders_daily
            ORDER BY date, leg
        """).fetchall()
    except Exception:
        h_rows = []
    h_dates = sorted({r[0] for r in h_rows})
    h_idx = {d: i for i, d in enumerate(h_dates)}
    h_series: dict[str, list[int]] = {"PT": [0] * len(h_dates), "YT": [0] * len(h_dates), "LP": [0] * len(h_dates)}
    h_source: dict[str, list[str]] = {"PT": [""] * len(h_dates), "YT": [""] * len(h_dates), "LP": [""] * len(h_dates)}
    for date, leg, n, src in h_rows:
        if leg in h_series and date in h_idx:
            i = h_idx[date]
            h_series[leg][i] = int(n or 0)
            h_source[leg][i] = src or ""
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
        "holdersGrowth": {
            "dates":   h_dates,
            "PT":      h_series["PT"],
            "YT":      h_series["YT"],
            "LP":      h_series["LP"],
            # Per-day data quality flag; UI can dim 'reconstructed' segments.
            "sources": {"PT": h_source["PT"], "YT": h_source["YT"], "LP": h_source["LP"]},
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
        **_build_wallet_leaderboard_and_aux(con),
    }


# ─── Users tab v2 additions ──────────────────────────────────────────────
# Wallet leaderboard (with current Active Holdings USD), cohort buckets,
# weekly retention, whale-event feed. Powers v1-parity Users sub-views.
def _build_wallet_leaderboard_and_aux(con: duckdb.DuckDBPyConnection) -> dict:
    wallets = con.execute("""
        SELECT wallet, total_holdings_usd, pt_usd, yt_usd, lp_usd,
               active_markets_held, unclaimed_usd, n_claims,
               n_swaps, total_volume_usd, lifetime_markets,
               n_buy_yt, n_sell_yt, n_buy_pt, n_sell_pt,
               first_seen::VARCHAR, last_seen::VARCHAR, active_span_days,
               is_likely_pool
        FROM main_analytics.wallet_summary
        ORDER BY total_holdings_usd DESC NULLS LAST, total_volume_usd DESC NULLS LAST
        LIMIT 2000
    """).fetchall()
    # Cohort thresholds matching v1: Whale ≥100K, Large ≥10K, Medium ≥1K, Small ≥100, Micro <100.
    cohort = con.execute("""
        WITH c AS (
          SELECT
            CASE WHEN total_holdings_usd >= 100000 THEN 'whale'
                 WHEN total_holdings_usd >= 10000  THEN 'large'
                 WHEN total_holdings_usd >= 1000   THEN 'medium'
                 WHEN total_holdings_usd >= 100    THEN 'small'
                 WHEN total_holdings_usd > 0       THEN 'micro'
                 ELSE 'zero' END AS cohort,
            total_holdings_usd, n_swaps, lifetime_markets, last_seen,
            n_buy_yt + n_sell_yt + n_buy_pt + n_sell_pt AS trades
          FROM main_analytics.wallet_summary
        )
        SELECT cohort,
               COUNT(*)                                                                        AS n,
               SUM(total_holdings_usd)                                                         AS total_holdings,
               SUM(trades)                                                                     AS trades,
               AVG(n_swaps)                                                                    AS avg_swaps,
               AVG(lifetime_markets)                                                           AS avg_markets,
               COUNT(*) FILTER (WHERE last_seen >= CURRENT_DATE - INTERVAL 30 DAY)             AS active_30d
        FROM c GROUP BY cohort
    """).fetchall()
    retention = con.execute("""
        SELECT week::VARCHAR, new_wallets, returning_wallets, total_active
        FROM main_analytics.retention_weekly ORDER BY week
    """).fetchall()
    whales = con.execute("""
        SELECT signature, date::VARCHAR, signer, market_key, ticker, platform,
               action, direction, notional_usd
        FROM main_analytics.whale_events
        ORDER BY notional_usd DESC
        LIMIT 200
    """).fetchall()
    return {
        "wallets": [
            {
                "wallet": w[0],
                "holdingUsd": float(w[1] or 0),
                "ptUsd": float(w[2] or 0),
                "ytUsd": float(w[3] or 0),
                "lpUsd": float(w[4] or 0),
                "activeMarkets": int(w[5] or 0),
                "unclaimedUsd": float(w[6] or 0),
                "nClaims": int(w[7] or 0),
                "nSwaps": int(w[8] or 0),
                "lifetimeVolumeUsd": float(w[9] or 0),
                "lifetimeMarkets": int(w[10] or 0),
                "actions": {
                    "buyYt": int(w[11] or 0), "sellYt": int(w[12] or 0),
                    "buyPt": int(w[13] or 0), "sellPt": int(w[14] or 0),
                },
                "firstSeen": w[15],
                "lastSeen": w[16],
                "activeSpanDays": int(w[17] or 0),
                "isPool": bool(w[18]),
            }
            for w in wallets
        ],
        "cohorts": [
            {
                "cohort": c[0],
                "count": int(c[1] or 0),
                "totalHoldingsUsd": float(c[2] or 0),
                "trades": int(c[3] or 0),
                "avgSwaps": float(c[4] or 0),
                "avgMarkets": float(c[5] or 0),
                "active30d": int(c[6] or 0),
            }
            for c in cohort
        ],
        "retention": {
            "weeks":     [r[0] for r in retention],
            "new":       [int(r[1] or 0) for r in retention],
            "returning": [int(r[2] or 0) for r in retention],
            "active":    [int(r[3] or 0) for r in retention],
        },
        "whaleEvents": [
            {
                "signature": w[0], "date": w[1], "signer": w[2],
                "market": w[3], "ticker": w[4], "platform": w[5],
                "action": w[6], "direction": w[7],
                "usd": float(w[8] or 0),
            }
            for w in whales
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
    # Exact entry implied-yield per (market, leg, holder) + market average,
    # from holder_entry_iy (volume-weighted over the holder's buy trades).
    try:
        entry_iy_rows = con.execute("""
            SELECT market_key, leg, holder, avg_entry_iy, n_buys,
                   market_avg_entry_iy, market_priced_holders
            FROM main_analytics.holder_entry_iy
        """).fetchall()
    except duckdb.Error:
        entry_iy_rows = []
    entry_iy_by_holder: dict[tuple, dict] = {}
    market_entry_iy: dict[str, dict] = {}
    for mk, leg, holder, aiy, nb, m_aiy, m_n in entry_iy_rows:
        entry_iy_by_holder[(mk, leg, holder)] = {"iy": float(aiy), "nBuys": int(nb)}
        market_entry_iy[f"{mk}:{leg}"] = {
            "avgEntryIy": float(m_aiy) if m_aiy is not None else None,
            "pricedHolders": int(m_n or 0),
        }
    out: dict[str, dict] = {}
    for mk, leg, owner, amount, share_pct, usd, total_bal, rk, price in rows:
        key = f"{mk}:{leg}"
        e = out.setdefault(key, {
            "market": mk, "leg": leg, "holders": 0,
            "totalBalance": float(total_bal or 0),
            "totalUsd": float((total_bal or 0) * (price or 0)),
            "avgEntryIy": (market_entry_iy.get(key) or {}).get("avgEntryIy"),
            "pricedHolders": (market_entry_iy.get(key) or {}).get("pricedHolders", 0),
            "top": [],
        })
        e["holders"] += 1
        eiy = entry_iy_by_holder.get((mk, leg, owner))
        e["top"].append({
            "owner": owner,
            "balance": float(amount or 0),
            "usd": float(usd or 0),
            "sharePct": float(share_pct or 0),
            "entryIy": eiy["iy"] if eiy else None,
            "entryBuys": eiy["nBuys"] if eiy else 0,
        })
    # nHolders from holders_snapshot (full count, not just top 500)
    counts = dict(con.execute("""
        SELECT market_key || ':' || leg, n_holders FROM main_analytics.holders_snapshot
    """).fetchall())
    for k, v in out.items():
        v["holders"] = int(counts.get(k, v["holders"]))
    # Per-market unique holder count — union of PT, YT, LP owners. A wallet
    # holding multiple legs of the same market counts as ONE market holder.
    unique_per_market = dict(con.execute("""
        WITH mint_legs AS (
            SELECT pt_mint AS mint, market_key FROM main_core.dim_markets WHERE pt_mint IS NOT NULL
            UNION ALL
            SELECT yt_mint, market_key FROM main_core.dim_markets WHERE yt_mint IS NOT NULL
            UNION ALL
            SELECT lp_mint, market_key FROM main_core.dim_markets WHERE lp_mint IS NOT NULL
        )
        SELECT ml.market_key, COUNT(DISTINCT h.owner) AS n
        FROM main_intermediate.int_holders_current h
        JOIN mint_legs ml USING (mint)
        GROUP BY ml.market_key
    """).fetchall())
    # Per-market historical holders by leg, from int_holders_per_market_daily.
    # Same active-market filter as the protocol-level chart — markets only
    # contribute on dates while unmatured. Shape per market:
    #   {dates: ["YYYY-MM-DD", ...], PT: [n, ...], YT: [...], LP: [...]}
    try:
        hist_rows = con.execute("""
            SELECT market_key, date::VARCHAR, leg, n_holders
            FROM main_intermediate.int_holders_per_market_daily
            ORDER BY market_key, date, leg
        """).fetchall()
    except Exception:
        hist_rows = []
    hist: dict[str, dict] = {}
    for mk, date, leg, n in hist_rows:
        e = hist.setdefault(mk, {"datesSet": set(), "PT": {}, "YT": {}, "LP": {}})
        e["datesSet"].add(date)
        if leg in ("PT", "YT", "LP"):
            e[leg][date] = int(n or 0)
    history_out: dict[str, dict] = {}
    for mk, e in hist.items():
        dates = sorted(e["datesSet"])
        history_out[mk] = {
            "dates": dates,
            "PT": [e["PT"].get(d, 0) for d in dates],
            "YT": [e["YT"].get(d, 0) for d in dates],
            "LP": [e["LP"].get(d, 0) for d in dates],
        }
    return {
        "meta": {"generatedAt": datetime.now(timezone.utc).isoformat(), "markets": len(out)},
        "byMarketLeg": out,
        "holdersByMarket": {k: int(v) for k, v in unique_per_market.items()},
        "historyByMarket": history_out,
    }


def build_unclaimed_yield_json(con: duckdb.DuckDBPyConnection) -> dict:
    """Per-(wallet, market) unclaimed YT yield. Sourced from
    main_analytics.unclaimed_yield which combines:
      - staged_underlying:   already in the position's claim buffer
      - unstaged_underlying: accrued in the vault, not yet staged (computed
        via canonical formula from yield_token_position.rs::calc_earned_sy)
    Output schema per entry:
      { marketKey, ytBalance, stagedUnderlying, unstagedUnderlying,
        unclaimedUnderlying, unclaimedUsd }
    """
    rows = con.execute("""
        SELECT wallet, market_key, yt_balance,
               staged_underlying, unstaged_underlying, clmm_underlying,
               unclaimed_underlying, unclaimed_usd
        FROM main_analytics.unclaimed_yield
    """).fetchall()
    by_wallet: dict[str, list[dict]] = {}
    for wallet, mk, yt, staged, unstaged, clmm, total, usd in rows:
        by_wallet.setdefault(wallet, []).append({
            "marketKey": mk,
            "ytBalance": float(yt or 0),
            "stagedUnderlying":    float(staged or 0),
            "unstagedUnderlying":  float(unstaged or 0),
            "clmmUnderlying":      float(clmm or 0),
            "unclaimedUnderlying": float(total or 0),
            "unclaimedUsd":        float(usd or 0),
        })
    return {
        "meta": {"generatedAt": datetime.now(timezone.utc).isoformat(),
                 "totalPositions": len(rows), "totalWallets": len(by_wallet)},
        "byWallet": by_wallet,
    }


def build_tranche_json(con: duckdb.DuckDBPyConnection) -> dict:
    """Tranche-market state snapshot, sourced from on-chain via extract_tranche_states.

    Reads the latest raw_tranche_states snapshot — decoded directly from
    XPTrnchoawi… ExponentTranchingMarket accounts using the Anchor IDL.
    See docs/ONYC_TRANCHES_PLAN.md for the full pipeline.

    Per-tranche timeseries (coverage / APY history) will land in a follow-up
    once we have ≥7 snapshots — implemented via an int_tranche_state_daily
    mart that this builder will read in addition to the current snapshot.
    """
    # Read from the int_tranche_apy_daily mart (joins raw_tranche_states +
    # raw_pool_state + computes coverage_ratio, lp_prices, underlying/sr/jr
    # APYs via window functions). Falls back to raw_tranche_states for
    # bonus on-chain fields the mart doesn't carry.
    rows = con.execute("""
        WITH latest AS (
            SELECT MAX(snapshot_date) AS d FROM main_intermediate.int_tranche_apy_daily
        )
        SELECT a.*, t.mint_lp_senior, t.mint_lp_junior, t.market_state,
               t.min_deposit_amount, t.last_distribution_ts
        FROM main_intermediate.int_tranche_apy_daily a, latest
        JOIN main.raw_tranche_states t
          ON t.tranche_vault = a.tranche_vault
         AND t.snapshot_date = a.snapshot_date
        WHERE a.snapshot_date = latest.d
    """).fetchall()
    cols = [d[0] for d in con.description]

    # Historical timeseries: per-vault daily snapshots ordered by date.
    # Returns: { vault_pk: { dates: [...], coverage: [...], sr_nav: [...],
    #                        jr_nav: [...], utilization: [...] } }
    history_rows = con.execute("""
        SELECT
            tranche_vault, snapshot_date::VARCHAR,
            sr_effective_nav_usd, jr_effective_nav_usd,
            sr_raw_nav_usd, jr_raw_nav_usd,
            utilization, current_junior_return_share
        FROM main.raw_tranche_states
        ORDER BY tranche_vault, snapshot_date
    """).fetchall()
    history: dict[str, dict] = {}
    for vault, dt, sr_eff, jr_eff, sr_raw, jr_raw, util, jr_share in history_rows:
        h = history.setdefault(vault, {
            "dates": [], "coverageRatio": [], "srNavUsd": [], "jrNavUsd": [],
            "utilizationPct": [], "currentJrReturnShare": [],
        })
        h["dates"].append(dt)
        total_eff = (sr_eff or 0) + (jr_eff or 0)
        h["coverageRatio"].append((jr_eff or 0) / total_eff if total_eff > 0 else None)
        h["srNavUsd"].append(sr_raw or 0)
        h["jrNavUsd"].append(jr_raw or 0)
        h["utilizationPct"].append((util or 0) * 100)
        h["currentJrReturnShare"].append(jr_share)

    # LP action aggregates per tranche vault (cumulative since vault genesis).
    lp_action_rows = con.execute("""
        SELECT tranche_vault, leg,
               COUNT(*)                                AS n_actions,
               COUNT(DISTINCT wallet)                   AS n_unique_wallets,
               SUM(CASE WHEN lp_amount_raw > 0 THEN 1 ELSE 0 END) AS n_deposits,
               SUM(CASE WHEN lp_amount_raw < 0 THEN 1 ELSE 0 END) AS n_withdrawals
        FROM main.raw_tranche_lp_actions
        GROUP BY 1, 2
    """).fetchall()
    actions_by_vault: dict[str, dict] = {}
    for vault, leg, n, n_wallets, n_dep, n_wd in lp_action_rows:
        actions_by_vault.setdefault(vault, {})[leg] = {
            "actions": n, "uniqueWallets": n_wallets,
            "deposits": n_dep, "withdrawals": n_wd,
        }

    # Top LPs per (vault, leg) — by cumulative net LP held (deposits − withdrawals).
    top_lp_rows = con.execute("""
        WITH net_per_wallet AS (
            SELECT tranche_vault, leg, wallet,
                   SUM(lp_amount_raw) / 1e9 AS net_lp_units
            FROM main.raw_tranche_lp_actions
            GROUP BY 1, 2, 3
            HAVING SUM(lp_amount_raw) > 0
        ),
        ranked AS (
            SELECT *,
                   ROW_NUMBER() OVER (
                       PARTITION BY tranche_vault, leg
                       ORDER BY net_lp_units DESC
                   ) AS rk
            FROM net_per_wallet
        )
        SELECT tranche_vault, leg, wallet, net_lp_units, rk
        FROM ranked
        WHERE rk <= 10
        ORDER BY tranche_vault, leg, rk
    """).fetchall()
    top_lps_by_vault: dict[str, dict[str, list[dict]]] = {}
    for vault, leg, wallet, net_lp, rk in top_lp_rows:
        d = top_lps_by_vault.setdefault(vault, {})
        d.setdefault(leg, []).append({"wallet": wallet, "lpUnits": float(net_lp)})

    # Recent activity per vault (newest 25 events across both legs).
    recent_rows = con.execute("""
        SELECT tranche_vault, signature, block_time, ix_name, wallet, leg,
               lp_amount_raw / 1e9 AS lp_units
        FROM main.raw_tranche_lp_actions
        ORDER BY tranche_vault, block_time DESC
    """).fetchall()
    recent_by_vault: dict[str, list[dict]] = {}
    for vault, sig, bt, ix, wallet, leg, lp_units in recent_rows:
        lst = recent_by_vault.setdefault(vault, [])
        if len(lst) >= 25:
            continue
        lst.append({
            "sig": sig,
            "blockTime": int(bt or 0),
            "action": ix,
            "wallet": wallet,
            "leg": leg,
            "lpUnits": float(lp_units),
        })

    # Return curve (50 breakpoints per vault — same for every snapshot today
    # but stored daily so we can detect ModifyMarket curve updates).
    curve_rows = con.execute("""
        SELECT tranche_vault, breakpoint_idx, x_value, y_value
        FROM main.raw_tranche_return_curves
        WHERE snapshot_date = (
            SELECT MAX(snapshot_date) FROM main.raw_tranche_return_curves
        )
        ORDER BY tranche_vault, breakpoint_idx
    """).fetchall()
    curve_by_vault: dict[str, list[dict]] = {}
    for vault, idx, x, y in curve_rows:
        curve_by_vault.setdefault(vault, []).append({"x": x, "y": y})

    # Need supply caps from raw_tranche_states (not in the apy mart).
    cap_rows = con.execute("""
        SELECT tranche_vault, max_sr_lp_supply, max_jr_lp_supply
        FROM main.raw_tranche_states
        WHERE snapshot_date = (SELECT MAX(snapshot_date) FROM main.raw_tranche_states)
    """).fetchall()
    cap_by_vault = {v: (sr, jr) for v, sr, jr in cap_rows}

    tranches = []
    for r in rows:
        row = dict(zip(cols, r))
        sr_nav        = row.get("sr_raw_nav_usd") or 0.0
        jr_nav        = row.get("jr_raw_nav_usd") or 0.0
        sr_eff        = row.get("sr_effective_nav_usd") or 0.0
        jr_eff        = row.get("jr_effective_nav_usd") or 0.0
        sr_lp_price   = row.get("sr_lp_price")
        jr_lp_price   = row.get("jr_lp_price")
        underlying_apy = row.get("underlying_apy")
        senior_apy    = row.get("senior_apy")
        junior_apy    = row.get("junior_apy")

        vault_pk      = row.get("tranche_vault")
        max_sr_raw, max_jr_raw = cap_by_vault.get(vault_pk, (0, 0))
        sr_max_ui     = (max_sr_raw or 0) / 1e9
        jr_max_ui     = (max_jr_raw or 0) / 1e9
        sr_max_usd    = (sr_max_ui * sr_lp_price) if sr_lp_price else None
        jr_max_usd    = (jr_max_ui * jr_lp_price) if jr_lp_price else None
        sr_rem_usd    = (sr_max_usd - sr_nav) if sr_max_usd is not None else None
        jr_rem_usd    = (jr_max_usd - jr_nav) if jr_max_usd is not None else None

        # Ticker + platform are derived from the seed_id today; can be enriched
        # via a side-table once more tranche vaults exist (currently 1: onycC101).
        seed = row.get("seed_id") or ""
        ticker = "ONyc" if seed.lower().startswith("onyc") else seed.split("C")[0].upper() or "?"

        tranches.append({
            "address":            vault_pk,
            "ticker":             ticker,
            "platform":           "OnRe" if ticker == "ONyc" else None,
            "seedId":             seed,
            "startDate":          None,  # not on the vault account
            "marketState":        row.get("market_state"),
            "underlyingApy":      underlying_apy,
            "underlyingApy7d":    underlying_apy,  # same source; window is implicit in lag(1)
            "underlyingApy30d":   underlying_apy,  # phase 4: separate 30d window calc
            "syExchangeRate":     row.get("current_sy_exchange_rate"),
            "coverageRatio":      row.get("coverage_ratio"),
            "minCoverageRatio":   row.get("min_coverage_ratio"),
            "marketSizeUsd":      sr_nav + jr_nav,
            "effectiveSizeUsd":   (sr_eff or 0) + (jr_eff or 0),
            "utilizationPct":     row.get("utilization_pct"),
            "senior": {
                "apy":            senior_apy,
                "navUsd":         sr_nav,
                "effectiveNavUsd":sr_eff,
                "lpPrice":        sr_lp_price,
                "maxCapacityUsd": sr_max_usd,
                "remainingUsd":   sr_rem_usd,
                "pointsMult":     None,  # not on-chain
                "lpMint":         row.get("mint_lp_senior"),
            },
            "junior": {
                "apy":            junior_apy,
                "navUsd":         jr_nav,
                "effectiveNavUsd":jr_eff,
                "lpPrice":        jr_lp_price,
                "maxCapacityUsd": jr_max_usd,
                "remainingUsd":   jr_rem_usd,
                "pointsMult":     None,
                "lpMint":         row.get("mint_lp_junior"),
            },
            "syMint":             row.get("sy_mint"),
            "baseMint":           None,
            # Bonus on-chain fields not exposed by the API:
            "currentJrReturnShare":      row.get("current_junior_return_share"),
            "twJrReturnShareAccrued":    row.get("tw_junior_return_share_accrued"),
            "lastSyncTs":                row.get("last_sync_ts"),
            "lastDistributionTs":        row.get("last_distribution_ts"),
            "fixedTermEndTs":            row.get("fixed_term_end_ts"),
            "fixedTermDurationSec":      row.get("fixed_term_duration_sec"),
            "minDepositAmount":          row.get("min_deposit_amount"),
            # Per-vault per-day timeseries; grows by 1 row per refresh.
            "history":                   history.get(vault_pk, {}),
            # Lifetime per-leg LP action aggregates (deposits, withdrawals,
            # unique wallets). Powers the "active LPs" widget.
            "lpActions":                 actions_by_vault.get(vault_pk, {}),
            # Top 10 LPs per leg by cumulative net LP units held.
            "topLps":                    top_lps_by_vault.get(vault_pk, {}),
            # Most-recent 25 deposit/withdraw events (newest first).
            "recentActivity":            recent_by_vault.get(vault_pk, []),
            # Piecewise-linear return curve (50 breakpoints) for plotting.
            "returnCurve":               curve_by_vault.get(vault_pk, []),
        })

    return {
        "meta": {
            "generatedAt":  datetime.now(timezone.utc).isoformat(),
            "source":       "warehouse: int_tranche_apy_daily (on-chain XPTrnchoawi vault + XP1BRL SY pool; APYs from time-series delta)",
            "count":        len(tranches),
            "snapshotDate": str(rows[0][0]) if rows else None,
        },
        "tranches": tranches,
    }


# Fallback (symbol, decimals) when raw_token_metadata is missing the mint.
# USD1 in particular has no metadata entry today, so hard-code it here.
_STRATEGY_VAULT_MINT_FALLBACK = {
    "So11111111111111111111111111111111111111112":  ("SOL",  9),
    "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v": ("USDC", 6),
    "USD1111111111111111111111111111111111111111":  ("USD", 6),
}

# Managed strategies listed on app.exponent.finance/en/strategy/<address>.
# The authoritative source is raw_strategy_vault_registry (Exponent's
# /strategies/managed API); this map is only the fallback when the registry
# extractor hasn't populated the warehouse yet.
_MANAGED_STRATEGY_NAMES = {
    "9iPUphFXxnyAKYnCTG3XZv5ybHv5Ki1diqA5mis3TBVB": "OnRe Growth",
    "puiRBGikahXM4C7weFtNRFcs7bSUx8wVn1m5v6RA91o":  "Raiku SOL Ecosystem Vault",
    "F7ekbk2uWGGvp71bJdmqs7P1d5xAt46qyMj2R5W4XEVR": "Solstice Yield Looping",
}

_REGISTRY_COLS = [
    "address", "name", "strategist", "profile", "description",
    "deposit_mint", "deposit_ticker",
    "apy_3d", "apy_7d", "apy_30d", "apy_current", "pt_weight",
    "lp_price", "tvl", "exchange_rate", "capacity",
]


def build_strategy_vault_json(con: duckdb.DuckDBPyConnection) -> dict:
    """Strategy-vault leaderboard + per-vault history, flows, holders, activity.

    One row per vault from mart_strategy_vault_leaderboard (latest snapshot).
    History pulled from mart_strategy_vault_timeseries (may be short while
    the backfill is still landing). Recent activity + flow aggregates come
    from mart_strategy_vault_flows (user actions only, tx_success=true).
    Top holders read from raw_strategy_vault_holders at the latest snapshot.

    underlyingSymbol/underlyingDecimals resolve from raw_token_metadata
    first, then fall back to _STRATEGY_VAULT_MINT_FALLBACK (currently just
    USD1, which has no metadata row).
    """
    # Leaderboard — one row per vault at latest snapshot.
    lb_rows = con.execute("""
        SELECT * FROM main_analytics.mart_strategy_vault_leaderboard
    """).fetchall()
    lb_cols = [d[0] for d in con.description]

    # Exponent's managed-strategy registry — curated names, strategist, and
    # THEIR windowed APYs (from their lpPrice history, matching the Exponent
    # frontend). Absent until the registry extractor has run.
    try:
        reg_rows = con.execute(f"""
            SELECT {", ".join(_REGISTRY_COLS)}
            FROM main.raw_strategy_vault_registry
            WHERE fetch_date = (SELECT MAX(fetch_date)
                                FROM main.raw_strategy_vault_registry)
        """).fetchall()
    except duckdb.Error:
        reg_rows = []
    registry = {r[0]: dict(zip(_REGISTRY_COLS, r)) for r in reg_rows}
    managed_order_keys = list(registry) if registry else list(_MANAGED_STRATEGY_NAMES)

    # Underlying + LP mint → (symbol, decimals). Prefer token_metadata; fall back.
    mint_rows = con.execute("""
        SELECT mint, symbol, decimals FROM main.raw_token_metadata
        WHERE mint IN (SELECT DISTINCT underlying_mint
                       FROM main_analytics.mart_strategy_vault_leaderboard
                       UNION
                       SELECT DISTINCT mint_lp
                       FROM main_analytics.mart_strategy_vault_leaderboard)
    """).fetchall()
    mint_info: dict[str, tuple[str, int]] = {}
    mint_decimals: dict[str, int] = {}
    for mint, sym, dec in mint_rows:
        if dec is not None:
            mint_decimals[mint] = int(dec)
        if sym is not None and dec is not None:
            mint_info[mint] = (sym, int(dec))

    def resolve_mint(mint: str | None) -> tuple[str | None, int]:
        if not mint:
            return None, 9
        if mint in mint_info:
            return mint_info[mint]
        if mint in _STRATEGY_VAULT_MINT_FALLBACK:
            return _STRATEGY_VAULT_MINT_FALLBACK[mint]
        return None, 9

    # LP-mint decimals per vault — from token metadata when indexed, else
    # assume the LP mint mirrors the underlying's decimals (observed on-chain:
    # deposits mint LP ≈ 1:1 with base at NAV ~1).
    lp_dec_by_vault: dict[str, int] = {}
    for r in lb_rows:
        row = dict(zip(lb_cols, r))
        _, u_dec = resolve_mint(row["underlying_mint"])
        lp_dec_by_vault[row["vault"]] = mint_decimals.get(row["mint_lp"], u_dec)

    # Per-vault daily history. total_aum = idle base + deployed positions.
    history_rows = con.execute("""
        SELECT vault, date::VARCHAR, total_aum, lp_balance, nav_per_share,
               apy_7d, apy_30d, net_flow_lp, unique_actors
        FROM main_analytics.mart_strategy_vault_timeseries
        ORDER BY vault, date
    """).fetchall()

    # Per-vault flow aggregates over the full mart window.
    flow_agg_rows = con.execute("""
        SELECT vault,
               COUNT(*) FILTER (WHERE ix_name = 'deposit_liquidity')            AS n_deposits,
               COUNT(*) FILTER (WHERE ix_name = 'queue_withdrawal')             AS n_queues,
               COUNT(*) FILTER (WHERE ix_name = 'execute_withdrawal')           AS n_executes,
               COUNT(*) FILTER (WHERE ix_name =
                                      'execute_withdrawal_from_reserves')       AS n_fast,
               COUNT(DISTINCT actor)                                            AS n_actors
        FROM main_analytics.mart_strategy_vault_flows
        GROUP BY vault
    """).fetchall()
    flow_agg_by_vault: dict[str, dict] = {}
    for vault, n_dep, n_q, n_ex, n_fw, n_act in flow_agg_rows:
        flow_agg_by_vault[vault] = {
            "totalDeposits":      int(n_dep or 0),
            "totalQueues":        int(n_q or 0),
            "totalExecutes":      int(n_ex or 0),
            "totalFastWithdraws": int(n_fw or 0),
            "uniqueActors":       int(n_act or 0),
        }

    # Position composition per vault, from the latest state snapshot:
    # token_entries (idle base) + strategy_positions TokenAccount balances,
    # labeled via the full token-metadata table (PT mints are indexed there).
    all_meta = {
        m: (s, int(dc)) for m, s, dc in con.execute(
            "SELECT mint, symbol, decimals FROM main.raw_token_metadata "
            "WHERE decimals IS NOT NULL"
        ).fetchall()
    }
    position_rows = con.execute("""
        SELECT vault, token_entries_json, strategy_positions_json
        FROM main.raw_strategy_vault_states
        WHERE snapshot_date = (SELECT MAX(snapshot_date)
                               FROM main.raw_strategy_vault_states)
    """).fetchall()

    # Kamino obligation snapshots (may be absent on warehouses where the
    # obligations extractor hasn't run yet — treat as no data).
    # mint → symbol for obligation legs (deposits/borrows reference reserve
    # liquidity mints resolved by the obligations extractor).
    _oblig_sym = dict(con.execute(
        "SELECT mint, symbol FROM main.raw_token_metadata WHERE symbol IS NOT NULL"
    ).fetchall())

    def _oblig_legs(legs_json: str | None) -> list[dict]:
        out = []
        for leg in json.loads(legs_json or "[]"):
            mv = float(leg.get("market_value") or 0)
            # amount is deposited_amount (deposits) or borrowed_amount (borrows)
            amt = float(leg.get("deposited_amount") or leg.get("borrowed_amount") or 0)
            if mv <= 0 and amt <= 0:
                continue
            mint = leg.get("mint")
            out.append({
                "symbol": _oblig_sym.get(mint) or (mint[:4] + "…" if mint else "?"),
                "valueUi": mv,
                # a freshly-deposited leg Kamino hasn't priced yet (real
                # position, market_value still 0) — flag so the UI can label it
                "ramping": mv <= 0 and amt > 0,
            })
        out.sort(key=lambda x: -x["valueUi"])
        return out

    try:
        oblig_rows = con.execute("""
            SELECT vault, obligation, collateral_value, debt_value,
                   allowed_borrow_value, unhealthy_borrow_value,
                   deposits_json, borrows_json
            FROM main.raw_strategy_vault_obligations
            WHERE snapshot_date = (SELECT MAX(snapshot_date)
                                   FROM main.raw_strategy_vault_obligations)
        """).fetchall()
    except duckdb.Error:
        oblig_rows = []
    obligs_by_vault: dict[str, list[dict]] = {}
    for (vault, obligation, coll, debt, allowed, unhealthy,
         deposits_json, borrows_json) in oblig_rows:
        coll = float(coll or 0)
        debt = float(debt or 0)
        if coll == 0 and debt == 0:
            continue
        net = coll - debt
        collateral_legs = _oblig_legs(deposits_json)
        obligs_by_vault.setdefault(vault, []).append({
            "obligation":      obligation,
            "collateralValue": coll,
            "debtValue":       debt,
            "netValue":        net,
            "ltv":             (debt / coll) if coll > 0 else None,
            "maxLtv":          (float(allowed or 0) / coll) if coll > 0 else None,
            "liqLtv":          (float(unhealthy or 0) / coll) if coll > 0 else None,
            "leverage":        (coll / net) if net > 0 else None,
            "collateral":      collateral_legs,
            "borrows":         _oblig_legs(borrows_json),
            "loopCount":       len(collateral_legs),
        })

    def _position_tokens(vault_pk: str, te_json: str | None,
                         sp_json: str | None, fallback_dec: int) -> list[dict]:
        """Nonzero token holdings: idle base entries + strategy token accounts."""
        out: list[dict] = []
        def add(mint: str, amount: int, kind: str) -> None:
            if not amount:
                return
            sym, dec = all_meta.get(mint, (None, fallback_dec))
            out.append({
                "mint":     mint,
                "symbol":   sym,
                "amountUi": amount / 10 ** dec,
                "kind":     kind,
            })
        try:
            for t in json.loads(te_json or "[]"):
                add(t.get("mint", ""), int(t.get("last_observed_amount") or 0), "idle")
            for p in json.loads(sp_json or "[]"):
                if p.get("variant") == "TokenAccount":
                    amt = sum(int(b.get("last_observed_amount") or 0)
                              for b in p.get("balances", []))
                    add(p.get("token_mint", ""), amt, "position")
        except (TypeError, json.JSONDecodeError):
            pass
        return out

    positions_json_by_vault = {v: (te, sp) for v, te, sp in position_rows}

    # Top 10 holders per vault at the latest snapshot.
    holder_rows = con.execute("""
        SELECT vault, holder, lp_amount
        FROM main.raw_strategy_vault_holders
        WHERE snapshot_date = (SELECT MAX(snapshot_date)
                               FROM main.raw_strategy_vault_holders)
        ORDER BY vault, lp_amount DESC
    """).fetchall()
    holders_by_vault: dict[str, list[dict]] = {}
    for vault, wallet, lp_amt in holder_rows:
        lst = holders_by_vault.setdefault(vault, [])
        if len(lst) >= 10:
            continue
        lst.append({
            "wallet":   wallet,
            "lpAmount": int(lp_amt or 0),
            "lpUi":     (int(lp_amt or 0)) / 10 ** lp_dec_by_vault.get(vault, 9),
        })

    # Deposit-implied NAV: every deposit mints LP at the prevailing NAV, so
    # deposit_token_amount_in / lp_delta is an exact NAV observation at that
    # block. Daily median per vault gives a NAV series back to inception —
    # crucial while the daily state-snapshot history is still short.
    # CAVEAT: routed/wrapper deposits carry sentinel-scale args (~1e19)
    # unrelated to the actual amount, so raw per-deposit observations are
    # collected here and filtered to a sane NAV band after per-vault decimal
    # rescaling below; the daily median is taken over surviving observations.
    implied_nav_rows = con.execute("""
        SELECT vault,
               action_date::DATE::VARCHAR          AS d,
               deposit_token_amount_in / lp_delta  AS nav_atoms
        FROM main_analytics.mart_strategy_vault_flows
        WHERE ix_name = 'deposit_liquidity'
          AND deposit_token_amount_in IS NOT NULL AND deposit_token_amount_in > 0
          AND lp_delta IS NOT NULL AND lp_delta >= 1000
        ORDER BY vault, d
    """).fetchall()
    implied_nav_obs: dict[str, dict[str, list[float]]] = {}
    for vault, d, nav_atoms in implied_nav_rows:
        implied_nav_obs.setdefault(vault, {}).setdefault(d, []).append(float(nav_atoms))

    # Recent activity: newest 25 user actions per vault. mart_strategy_vault_flows
    # is already filtered to user actions with tx_success=true and ordered by
    # block_time DESC — we just cap per-vault in Python.
    activity_rows = con.execute("""
        SELECT vault, signature, block_time, actor, ix_name,
               lp_delta, base_delta,
               deposit_token_amount_in, queue_lp_amount, fast_withdraw_lp_amount
        FROM main_analytics.mart_strategy_vault_flows
        ORDER BY vault, block_time DESC
    """).fetchall()
    managed_addrs = set(registry) | set(_MANAGED_STRATEGY_NAMES)

    # Vault-side operations for the /strategy/ detail page: manager position
    # updates, policy changes, governance proposal cycles, and withdrawal
    # fills (fill_withdrawal is manager-executed even though the flows mart
    # groups it with user actions). Keeper noise — refresh_aum, oracle
    # cranks, interaction hooks, policy syncs — is excluded.
    manager_activity_rows = con.execute("""
        SELECT vault, signature, block_time, actor, ix_name, lp_delta, base_delta
        FROM main_staging.stg_strategy_vault_actions
        WHERE tx_success
          AND (NOT is_user_action OR ix_name = 'fill_withdrawal')
          AND NOT is_oracle_crank
          AND ix_name NOT IN ('refresh_aum', 'validate_interaction_hook',
                              'sync_policy_authorities', 'anchor_event_cpi',
                              'unknown')
        ORDER BY vault, block_time DESC
    """).fetchall()
    manager_activity_by_vault: dict[str, list[dict]] = {}
    for vault, sig, bt, actor, ix_name, lp_delta, base_delta in manager_activity_rows:
        if vault not in managed_addrs:
            continue
        lst = manager_activity_by_vault.setdefault(vault, [])
        if len(lst) >= 50:
            continue
        lst.append({
            "sig":       sig,
            "blockTime": int(bt or 0),
            "actor":     actor,
            "ixName":    ix_name,
            "lpDelta":   float(lp_delta or 0),
            "baseDelta": float(base_delta or 0),
            "argsJson":  None,
        })

    activity_by_vault: dict[str, list[dict]] = {}
    for (vault, sig, bt, actor, ix_name, lp_delta, base_delta,
         dep_amt, q_amt, fw_amt) in activity_rows:
        lst = activity_by_vault.setdefault(vault, [])
        # Managed vaults power the /strategy/ detail page's transaction
        # list — keep a deeper window for them.
        if len(lst) >= (100 if vault in managed_addrs else 25):
            continue
        # Pack typed args into a compact JSON string (only non-null fields).
        args = {}
        if dep_amt is not None: args["depositTokenAmountIn"] = int(dep_amt)
        if q_amt   is not None: args["queueLpAmount"]        = int(q_amt)
        if fw_amt  is not None: args["fastWithdrawLpAmount"] = int(fw_amt)
        lst.append({
            "sig":       sig,
            "blockTime": int(bt or 0),
            "actor":     actor,
            "ixName":    ix_name,
            "lpDelta":   float(lp_delta or 0),
            "baseDelta": float(base_delta or 0),
            "argsJson":  json.dumps(args, separators=(",", ":")) if args else None,
        })

    # Assemble per-vault history dicts.
    history_by_vault: dict[str, dict] = {}
    for (vault, dt, aum_base, lp_bal, nav, apy7, apy30, net_flow, uniq) in history_rows:
        h = history_by_vault.setdefault(vault, {
            "dates": [], "aumUi": [], "lpBalance": [], "navPerShare": [],
            "apy7d": [], "apy30d": [], "netFlowLp": [], "uniqueActors": [],
        })
        h["dates"].append(dt)
        h["lpBalance"].append(float(lp_bal or 0))
        h["navPerShare"].append(float(nav) if nav is not None else None)
        h["apy7d"].append(float(apy7) if apy7 is not None else None)
        h["apy30d"].append(float(apy30) if apy30 is not None else None)
        h["netFlowLp"].append(float(net_flow or 0))
        h["uniqueActors"].append(int(uniq or 0))
        # aumUi depends on underlying decimals — patched below once we know them.
        h.setdefault("_aumBase", []).append(float(aum_base or 0))

    vaults = []
    snapshot_date_out = None
    for r in lb_rows:
        row = dict(zip(lb_cols, r))
        vault_pk = row["vault"]
        underlying_mint = row["underlying_mint"]
        symbol, decimals = resolve_mint(underlying_mint)

        scale = 10 ** decimals
        # Headline AUM is total NAV: idle base tokens + capital deployed
        # into strategy positions (net of debt).
        aum_base = float(row["total_aum"] or 0)
        aum_ui = aum_base / scale
        idle_ui = float(row["aum_in_base"] or 0) / scale
        deployed_ui = float(row["aum_in_base_in_positions"] or 0) / scale
        lp_dec = lp_dec_by_vault.get(vault_pk, decimals)

        # Rescale history aumUi with this vault's decimals, then drop the
        # helper base array.
        h = history_by_vault.get(vault_pk, {})
        if h:
            h["aumUi"] = [v / scale for v in h.pop("_aumBase", [])]

        snap_date = row["snapshot_date"]
        if snapshot_date_out is None and snap_date is not None:
            snapshot_date_out = str(snap_date)

        reg = registry.get(vault_pk)

        # Rescale implied-NAV atoms ratio to UI units:
        # (amt/10^u_dec) / (lp/10^lp_dec) = atoms_ratio * 10^(lp_dec - u_dec)
        # then discard sentinel-arg outliers — NAV starts at 1.0 and drifts
        # by yield, so anything outside [0.2, 5] is a routed/garbage arg,
        # not a real mint ratio. Median over survivors per day.
        # The ratio is deposit-token per LP, so it only tracks NAV when the
        # deposit token is pegged ~1:1 to the quote unit — skip vaults whose
        # deposit asset floats against the quote (e.g. rkuSOL vs SOL), where
        # the series would conflate LST appreciation with vault performance.
        implied = None
        deposit_pegged = (
            reg is None
            or reg["exchange_rate"] is None
            or abs(float(reg["exchange_rate"]) - 1) <= 0.005
        )
        obs = implied_nav_obs.get(vault_pk) if deposit_pegged else None
        if obs:
            nav_scale = 10 ** (lp_dec - decimals)
            dates, navs = [], []
            for day in sorted(obs):
                vals = sorted(
                    v * nav_scale for v in obs[day]
                    if 0.2 <= v * nav_scale <= 5.0
                )
                if not vals:
                    continue
                mid = len(vals) // 2
                dates.append(day)
                navs.append(vals[mid] if len(vals) % 2 else (vals[mid - 1] + vals[mid]) / 2)
            if dates:
                implied = {"dates": dates, "nav": navs}

        te_json, sp_json = positions_json_by_vault.get(vault_pk, (None, None))
        is_managed = vault_pk in registry or (not registry and vault_pk in _MANAGED_STRATEGY_NAMES)

        vaults.append({
            "address":                vault_pk,
            "name":                   (reg or {}).get("name") or _MANAGED_STRATEGY_NAMES.get(vault_pk),
            "isManaged":              is_managed,
            "managedOrder":           managed_order_keys.index(vault_pk) if vault_pk in managed_order_keys else None,
            "strategist":             (reg or {}).get("strategist"),
            "profile":                (reg or {}).get("profile"),
            "description":            (reg or {}).get("description"),
            "depositTicker":          (reg or {}).get("deposit_ticker"),
            "capacityUi":             (reg or {}).get("capacity"),
            "exponentApy": None if not reg else {
                "d3":       reg["apy_3d"],
                "d7":       reg["apy_7d"],
                "d30":      reg["apy_30d"],
                "current":  reg["apy_current"],
                "ptWeight": reg["pt_weight"],
            },
            "positions": {
                "tokens":      _position_tokens(vault_pk, te_json, sp_json, decimals),
                "obligations": obligs_by_vault.get(vault_pk, []),
            },
            "underlyingMint":         underlying_mint,
            "underlyingSymbol":       symbol,
            "underlyingDecimals":     decimals,
            "mintLp":                 row["mint_lp"],
            "squadsVault":            row["manager"],
            "aumInBase":              aum_base,
            "aumUi":                  aum_ui,
            "idleUi":                 idle_ui,
            "deployedUi":             deployed_ui,
            "lpBalance":              float(row["lp_balance"] or 0),
            "lpDecimals":             lp_dec,
            "lpBalanceUi":            float(row["lp_balance"] or 0) / 10 ** lp_dec,
            "navPerShare":            float(row["nav_per_share"]) if row["nav_per_share"] is not None else None,
            "pctFull":                float(row["pct_full"]) if row["pct_full"] is not None else None,
            "deployedRatio":          float(row["deployed_ratio"]) if row["deployed_ratio"] is not None else None,
            "idleRatio":              float(row["idle_ratio"]) if row["idle_ratio"] is not None else None,
            "fastExitUtilization":    float(row["fast_exit_utilization"]) if row["fast_exit_utilization"] is not None else None,
            "pendingWithdrawRatio":   float(row["pending_withdraw_ratio"]) if row["pending_withdraw_ratio"] is not None else None,
            # On-chain management_fee_bps is misnamed: the u64 is the fee
            # fraction × 1e6 (ppm), NOT bps — verified against Exponent's
            # API (Solstice raw 15000 ↔ managementFee 0.015 = 150 bps).
            # ÷100 converts ppm → true bps so the UI's fmtBp renders right.
            "managementFeeBps":       (row["management_fee_bps"] / 100)
                                      if row["management_fee_bps"] is not None else None,
            "normalWithdrawalCutBp":  row["normal_withdrawal_cut_bp"],
            "fastWithdrawalCutBp":    row["fast_withdrawal_cut_bp"],
            "snapshotDate":           str(snap_date) if snap_date is not None else None,
            "history":                h,
            "impliedNav":             implied,
            "flows":                  flow_agg_by_vault.get(vault_pk, {
                "totalDeposits": 0, "totalQueues": 0, "totalExecutes": 0,
                "totalFastWithdraws": 0, "uniqueActors": 0,
            }),
            "topHolders":             holders_by_vault.get(vault_pk, []),
            "recentActivity":         activity_by_vault.get(vault_pk, []),
            "managerActivity":        manager_activity_by_vault.get(vault_pk, []),
        })

    return {
        "meta": {
            "generatedAt":  datetime.now(timezone.utc).isoformat(),
            "source":       "extract_strategy_vault_states + backfill",
            "count":        len(vaults),
            "snapshotDate": snapshot_date_out,
        },
        "vaults": vaults,
    }


def build_strategy_txn_shards(con: duckdb.DuckDBPyConnection) -> None:
    """Emit one JSON per managed strategy under web/public/strategy-txns/.

    The /strategy/ detail page fetches these for the FULL transaction
    history (strategy_vault.json only embeds a capped window as fallback).
    userActivity mirrors mart_strategy_vault_flows minus fill_withdrawal;
    vaultActivity is manager/governance ops with keeper noise excluded,
    argsJson passed through (init_proposal carries proposal_id). roles maps
    known operator addresses so the UI can label actors.
    """
    shard_dir = WEB_PUBLIC / "strategy-txns"
    if shard_dir.exists():
        for f in shard_dir.glob("*.json"):
            f.unlink()
    shard_dir.mkdir(parents=True, exist_ok=True)

    try:
        reg_rows = con.execute("""
            SELECT address, payload_json
            FROM main.raw_strategy_vault_registry
            WHERE fetch_date = (SELECT MAX(fetch_date)
                                FROM main.raw_strategy_vault_registry)
        """).fetchall()
    except duckdb.Error:
        reg_rows = []
    payload_by_addr = {a: json.loads(p or "{}") for a, p in reg_rows}
    managed = set(payload_by_addr) | set(_MANAGED_STRATEGY_NAMES)

    # Label lookups for proposal action summaries: mint → symbol from token
    # metadata; program id → protocol name from every registry protocols list
    # (plus the vault program itself).
    sym_by_mint = dict(con.execute(
        "SELECT mint, symbol FROM main.raw_token_metadata WHERE symbol IS NOT NULL"
    ).fetchall())
    protocol_names: dict[str, str] = {
        "sVau1tXvayVWfotzm9Ahcv2qfnnfRWttt78BCnNC6dD": "Strategy Vault",
    }
    for p in payload_by_addr.values():
        for proto in p.get("protocols") or []:
            if proto.get("programId") and proto.get("name"):
                protocol_names[proto["programId"]] = proto["name"]

    # Per-vault NAV context for valuing the withdrawal queue.
    state_rows = con.execute("""
        SELECT vault, mint_lp, underlying_mint,
               coalesce(aum_in_base, 0) + coalesce(aum_in_base_in_positions, 0),
               lp_balance, pending_withdrawal_backlog_due_at
        FROM main.raw_strategy_vault_states
        WHERE snapshot_date = (SELECT MAX(snapshot_date)
                               FROM main.raw_strategy_vault_states)
    """).fetchall()
    dec_by_mint = {
        m: int(dc) for m, dc in con.execute(
            "SELECT mint, decimals FROM main.raw_token_metadata WHERE decimals IS NOT NULL"
        ).fetchall()
    }
    state_by_vault = {}
    for vault, mint_lp, u_mint, total_aum, lp_bal, due_at in state_rows:
        u_dec = dec_by_mint.get(u_mint, _STRATEGY_VAULT_MINT_FALLBACK.get(u_mint, (None, 9))[1])
        lp_dec = dec_by_mint.get(mint_lp, u_dec)
        state_by_vault[vault] = {
            "lp_dec": lp_dec,
            "u_dec": u_dec,
            "nav": (float(total_aum) / float(lp_bal)) if lp_bal else None,
            "lp_supply": float(lp_bal or 0),
            "due_at": int(due_at or 0),
        }

    try:
        wd_rows = con.execute("""
            SELECT vault, owner, lp_amount_requested, lp_amount_remaining, created_at
            FROM main.raw_strategy_vault_withdrawals
            WHERE lp_amount_remaining > 0
            ORDER BY vault, lp_amount_remaining DESC
        """).fetchall()
    except duckdb.Error:
        wd_rows = []
    wd_by_vault: dict[str, list[tuple]] = {}
    for r in wd_rows:
        wd_by_vault.setdefault(r[0], []).append(r)

    try:
        proposal_rows = con.execute("""
            SELECT vault, proposal_id, proposer, status, created_at,
                   voting_ends_at, executable_at, reject_votes,
                   opt_out_votes, lp_supply_snapshot, actions_json, parse_error
            FROM main.raw_strategy_vault_proposals
            ORDER BY vault, proposal_id DESC
        """).fetchall()
    except duckdb.Error:
        proposal_rows = []
    proposals_by_vault: dict[str, list[dict]] = {}
    for (vault, pid, proposer, status, created, ends, executable,
         reject, optout, lp_snap, actions_json, perr) in proposal_rows:
        actions = []
        for s in json.loads(actions_json or "[]"):
            assets, protocols = [], []
            for pk in s.get("pubkeys", []):
                if pk in protocol_names:
                    protocols.append(protocol_names[pk])
                elif pk in sym_by_mint:
                    assets.append(sym_by_mint[pk])
            protocols = list(dict.fromkeys(protocols))
            # Policy hooks always reference the vault program itself — only
            # worth showing when no external protocol is involved.
            if len(protocols) > 1 and "Strategy Vault" in protocols:
                protocols.remove("Strategy Vault")
            actions.append({
                "name":      s.get("name"),
                "mode":      s.get("mode"),
                "assets":    list(dict.fromkeys(assets)),
                "protocols": protocols,
                "verbs":     s.get("verbs", []),
            })
        proposals_by_vault.setdefault(vault, []).append({
            "id":            int(pid),
            "proposer":      proposer,
            "status":        status,
            "createdAt":     int(created or 0),
            "votingEndsAt":  int(ends or 0),
            "executableAt":  int(executable or 0),
            "rejectVotes":   float(reject or 0),
            "optOutVotes":   float(optout or 0),
            "lpSnapshot":    float(lp_snap or 0),
            "actions":       actions,
            "parseError":    bool(perr),
        })

    for addr in sorted(managed):
        p = payload_by_addr.get(addr, {})
        roles = {
            "admin":      p.get("adminAddress"),
            "sentinel":   p.get("sentinelAddress"),
            "strategist": (p.get("strategist") or {}).get("address"),
            "depositors": p.get("depositorsAddress"),
        }

        user_rows = con.execute("""
            SELECT signature, block_time, actor, ix_name, lp_delta, base_delta,
                   deposit_token_amount_in, queue_lp_amount, fast_withdraw_lp_amount
            FROM main_analytics.mart_strategy_vault_flows
            WHERE vault = ? AND ix_name != 'fill_withdrawal'
            ORDER BY block_time DESC
        """, [addr]).fetchall()
        user_activity = []
        for sig, bt, actor, ix_name, lp_d, base_d, dep, q, fw in user_rows:
            args = {}
            if dep is not None: args["depositTokenAmountIn"] = int(dep)
            if q   is not None: args["queueLpAmount"]        = int(q)
            if fw  is not None: args["fastWithdrawLpAmount"] = int(fw)
            user_activity.append({
                "sig":       sig,
                "blockTime": int(bt or 0),
                "actor":     actor,
                "ixName":    ix_name,
                "lpDelta":   float(lp_d or 0),
                "baseDelta": float(base_d or 0),
                "argsJson":  json.dumps(args, separators=(",", ":")) if args else None,
            })

        vault_rows = con.execute("""
            SELECT signature, block_time, actor, ix_name, lp_delta, base_delta,
                   args_json
            FROM main_staging.stg_strategy_vault_actions
            WHERE vault = ? AND tx_success
              AND (NOT is_user_action OR ix_name = 'fill_withdrawal')
              AND NOT is_oracle_crank
              AND ix_name NOT IN ('refresh_aum', 'validate_interaction_hook',
                                  'sync_policy_authorities', 'anchor_event_cpi',
                                  'unknown')
            ORDER BY block_time DESC
        """, [addr]).fetchall()
        vault_activity = [{
            "sig":       sig,
            "blockTime": int(bt or 0),
            "actor":     actor,
            "ixName":    ix_name,
            "lpDelta":   float(lp_d or 0),
            "baseDelta": float(base_d or 0),
            "argsJson":  args_json,
        } for sig, bt, actor, ix_name, lp_d, base_d, args_json in vault_rows]

        # Execution legs (manager's actual DeFi activity), newest first.
        try:
            exec_rows = con.execute("""
                SELECT signature, block_time, types, program_ids,
                       asset_mint, amount_ui, deltas_json
                FROM main.raw_strategy_vault_executions
                WHERE vault = ?
                ORDER BY block_time DESC
            """, [addr]).fetchall()
        except duckdb.Error:
            exec_rows = []
        executions = []
        for sig, bt, types, pids, mint, amt, deltas_json in exec_rows:
            protos = list(dict.fromkeys(
                protocol_names[p] for p in (pids or "").split(",")
                if p in protocol_names and protocol_names[p] != "Strategy Vault"
            ))
            type_list = [t for t in (types or "").split(",") if t]
            deltas = json.loads(deltas_json or "[]")
            symbol = sym_by_mint.get(mint) or (f"{mint[:4]}…" if mint else None)
            amount = float(amt) if amt is not None else None

            # Presentation to match Exponent's activity view:
            # - a PT-token delta via the CLMM/orderbook is a PT trade, not
            #   its mechanical LP / SY legs;
            # - for swaps, show the asset RECEIVED (largest positive delta).
            if symbol and symbol.startswith("PT-") and any(
                    p in ("Exponent CLMM", "Exponent Orderbook") for p in protos):
                type_list = ["Trade PT"] + [t for t in type_list
                                            if t not in ("LP", "Deposit SY", "Withdraw SY", "Trade PT")]
            if "Swap" in type_list:
                received = max((d for d in deltas if d["amountUi"] > 0),
                               key=lambda d: d["amountUi"], default=None)
                if received:
                    symbol = sym_by_mint.get(received["mint"], f"{received['mint'][:4]}…")
                    amount = received["amountUi"]

            executions.append({
                "sig":       sig,
                "blockTime": int(bt or 0),
                "types":     type_list,
                "protocols": protos,
                "symbol":    symbol,
                "amountUi":  amount,
                "deltas":    [{"symbol": sym_by_mint.get(d["mint"], f"{d['mint'][:4]}…"),
                               "amountUi": d["amountUi"]} for d in deltas[:6]],
            })

        # Reject threshold: registry rejectionThresholdBps is 1e-6-scale
        # (300000 → 30%).
        threshold_bps = p.get("rejectionThresholdBps")
        reject_threshold = (float(threshold_bps) / 1e6) if threshold_bps else None

        # Withdrawal queue summary: open requests, LP + base value leaving,
        # and the on-chain backlog due date.
        st = state_by_vault.get(addr, {})
        lp_dec = st.get("lp_dec", 9)
        nav = st.get("nav")
        open_reqs = wd_by_vault.get(addr, [])
        lp_remaining_ui = sum(r[3] for r in open_reqs) / 10 ** lp_dec
        withdrawal_queue = {
            "count":          len(open_reqs),
            "lpRequestedUi":  sum(r[2] for r in open_reqs) / 10 ** lp_dec,
            "lpRemainingUi":  lp_remaining_ui,
            "valueUi":        (lp_remaining_ui * nav) if nav else None,
            "pctOfSupply":    (sum(r[3] for r in open_reqs) / st["lp_supply"])
                              if st.get("lp_supply") else None,
            "oldestCreatedAt": min((int(r[4]) for r in open_reqs), default=None),
            "dueAt":          st.get("due_at") or None,
            "requests": [{
                "owner":     owner,
                "lpUi":      remaining / 10 ** lp_dec,
                "valueUi":   (remaining / 10 ** lp_dec * nav) if nav else None,
                "createdAt": int(created),
            } for _, owner, _, remaining, created in open_reqs[:10]],
        }

        _write_atomic(shard_dir / f"{addr}.json", {
            "address":         addr,
            "generatedAt":     datetime.now(timezone.utc).isoformat(),
            "roles":           roles,
            "rejectThreshold": reject_threshold,
            "withdrawalQueue": withdrawal_queue,
            "proposals":       proposals_by_vault.get(addr, []),
            "userActivity":    user_activity,
            "vaultActivity":   vault_activity,
            "executions":      executions,
        })
        rprint(f"  strategy-txns/{addr[:8]}…json  "
               f"user={len(user_activity)} vault={len(vault_activity)} "
               f"proposals={len(proposals_by_vault.get(addr, []))} "
               f"executions={len(executions)}")


def build_v2_orderbook_json(con: duckdb.DuckDBPyConnection) -> dict:
    """v2 (XPBook) orderbook snapshot per market.

    Schema:
      meta: { generatedAt, snapshotDate }
      byMarket: {
        [market_key]: {
          ticker, platform, maturityDate, syMint,
          primaryBookAccount, bookCount,
          nOffers, nBids, nAsks,
          bestBidApy, bestAskApy, midApy, spreadBps,
          totalBidSizeSy, totalAskSizeSy,
          books: [{ bookAccount, nOffers, nBids, nAsks,
                     bestBidApy, bestAskApy,
                     bids:[...], asks:[...] }]
        }
      }
    """
    summary_rows = con.execute("""
        SELECT
            market_key, ticker, platform, sy_mint, snapshot_date,
            primary_book_account, book_count,
            n_offers, n_bids, n_asks,
            best_bid_apy, best_ask_apy, mid_apy, spread_bps,
            total_bid_size_sy, total_ask_size_sy
        FROM main_analytics.v2_book_summary
    """).fetchall()

    ladder_rows = con.execute("""
        SELECT
            market_key, book_account, side, rk,
            owner_wallet, apy_rate, size_sy, size_sy_atomic,
            expiry_at, created_at, virtual_offer
        FROM main_analytics.v2_orderbook_top
        ORDER BY market_key, book_account, side, rk
    """).fetchall()

    market_dates = con.execute("""
        SELECT
            m.market_key,
            cast(to_timestamp(m.maturity_ts) as date) as maturity_date
        FROM main.raw_v2_markets m
    """).fetchall()
    maturity_by_market = {mk: str(d) for mk, d in market_dates}

    # Per-book ladder index
    per_book: dict[tuple[str, str], dict[str, list[dict]]] = {}
    per_book_summary: dict[tuple[str, str], dict] = {}
    for (mk, ba, side, rk, owner, apy, sz, sz_atom, expiry, created, virt) in ladder_rows:
        key = (mk, ba)
        per_book.setdefault(key, {"bids": [], "asks": []})
        per_book_summary.setdefault(key, {
            "bookAccount": ba, "nBids": 0, "nAsks": 0,
            "bestBidApy": None, "bestAskApy": None,
        })
        row = {
            "owner": owner,
            "apy": float(apy),
            "sizeSy": float(sz),
            "sizeSyAtomic": str(sz_atom),
            "createdAt": int(created or 0),
            "expiryAt": int(expiry or 0),
            "virtualOffer": int(virt or 0),
        }
        if side == "bid":
            per_book[key]["bids"].append(row)
            s = per_book_summary[key]
            s["nBids"] += 1
            if s["bestBidApy"] is None or row["apy"] > s["bestBidApy"]:
                s["bestBidApy"] = row["apy"]
        else:
            per_book[key]["asks"].append(row)
            s = per_book_summary[key]
            s["nAsks"] += 1
            if s["bestAskApy"] is None or row["apy"] < s["bestAskApy"]:
                s["bestAskApy"] = row["apy"]

    by_market: dict[str, dict] = {}
    snapshot_date_iso = None
    for (mk, ticker, platform, sy_mint, snap_date,
         primary_book, book_count, n_offers, n_bids, n_asks,
         bb, ba, mid, spread, bid_sz, ask_sz) in summary_rows:
        snapshot_date_iso = str(snap_date) if snapshot_date_iso is None else snapshot_date_iso
        books_list: list[dict] = []
        # Find all books for this market
        market_books = [k for k in per_book.keys() if k[0] == mk]
        # Sort: primary first, then by descending bids
        market_books.sort(key=lambda k: (k[1] != primary_book, -per_book_summary[k]["nBids"]))
        for k in market_books:
            ladder = per_book[k]
            s = per_book_summary[k]
            books_list.append({
                "bookAccount": s["bookAccount"],
                "nOffers": s["nBids"] + s["nAsks"],
                "nBids": s["nBids"],
                "nAsks": s["nAsks"],
                "bestBidApy": s["bestBidApy"],
                "bestAskApy": s["bestAskApy"],
                "bids": ladder["bids"],
                "asks": ladder["asks"],
            })
        by_market[mk] = {
            "ticker": ticker,
            "platform": platform or "",
            "maturityDate": maturity_by_market.get(mk),
            "syMint": sy_mint,
            "primaryBookAccount": primary_book,
            "bookCount": int(book_count or 1),
            "nOffers": int(n_offers or 0),
            "nBids": int(n_bids or 0),
            "nAsks": int(n_asks or 0),
            "bestBidApy": float(bb) if bb is not None else None,
            "bestAskApy": float(ba) if ba is not None else None,
            "midApy":     float(mid) if mid is not None else None,
            "spreadBps":  float(spread) if spread is not None else None,
            "totalBidSizeSy": float(bid_sz or 0),
            "totalAskSizeSy": float(ask_sz or 0),
            "books": books_list,
        }

    return {
        "meta": {
            "generatedAt": datetime.now(timezone.utc).isoformat(),
            "snapshotDate": snapshot_date_iso,
            "marketCount": len(by_market),
        },
        "byMarket": by_market,
    }


def build_wallet_shards(con: duckdb.DuckDBPyConnection) -> None:
    """Emit one JSON per wallet under web/public/wallet/{addr}.json.

    Restricted to wallets the UI actually links to — leaderboard +
    market top-holders + whale signers — so Vercel ships ~50 MB of
    shards instead of 220 MB for 31K mostly-unreachable wallets. The
    wallet detail page already renders a "no timeline data" fallback
    on 404 for wallets that aren't sharded.
    """
    shard_dir = WEB_PUBLIC / "wallet"
    if shard_dir.exists():
        for f in shard_dir.glob("*.json"):
            f.unlink()
    shard_dir.mkdir(parents=True, exist_ok=True)

    referenced: set[str] = set()
    try:
        with open(WEB_PUBLIC / "users.json") as f:
            u = json.load(f)
        for w in u.get("topWallets", []):  referenced.add(w.get("signer"))
        for w in u.get("wallets",    []):  referenced.add(w.get("wallet"))
        for w in u.get("whaleEvents",[]):  referenced.add(w.get("signer"))
        with open(WEB_PUBLIC / "market_holders.json") as f:
            mh = json.load(f)
        for _, e in (mh.get("byMarketLeg") or {}).items():
            for t in (e.get("top") or [])[:200]:
                referenced.add(t.get("owner"))
        with open(WEB_PUBLIC / "holders.json") as f:
            h = json.load(f)
        for r in h.get("rows") or []:
            referenced.add(r.get("wallet"))
    except FileNotFoundError as e:
        rprint(f"[yellow]  warn: {e.filename} not built yet; shipping all wallets[/yellow]")
        referenced = set()
    referenced.discard(None)
    rprint(f"  UI-referenced wallets: {len(referenced):,}")

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

    # Current positions per wallet — derived from int_holders_current for
    # PT/YT, and from int_clmm_lp_usd for CLMM LP positions (where the raw
    # lp_mint balance is a V3 liquidity-unit number, NOT a token balance,
    # and would mislead the UI by ~200× if shown as-is).
    rprint("  attaching current positions to each wallet…")
    pos_rows = con.execute("""
        SELECT h.owner, m.market_key, m.ticker, m.maturity_date::VARCHAR,
               CASE WHEN m.pt_mint = h.mint THEN 'PT'
                    WHEN m.yt_mint = h.mint THEN 'YT'
               END AS leg,
               h.amount
        FROM main_intermediate.int_holders_current h
        JOIN main_core.dim_markets m
          ON h.mint IN (m.pt_mint, m.yt_mint)
        WHERE h.amount > 0.000001
    """).fetchall()
    # CLMM LP rows: one per (wallet, market_key), summing pt/sy underlying
    # and USD across the wallet's positions in that market. balance is in
    # underlying-token units (pt_underlying + sy_underlying), matching what
    # the user would receive on a full burn.
    clmm_lp_rows = con.execute("""
        SELECT u.wallet, u.market_key, m.ticker, m.maturity_date::VARCHAR,
               SUM(u.pt_underlying + u.sy_underlying)  AS balance_underlying,
               SUM(u.lp_usd)                            AS lp_usd
        FROM main_intermediate.int_clmm_lp_usd u
        JOIN main_core.dim_markets m USING (market_key)
        WHERE u.market_key IS NOT NULL
        GROUP BY u.wallet, u.market_key, m.ticker, m.maturity_date
    """).fetchall()
    positions_by_wallet: dict[str, list[dict]] = {}
    for owner, mk, ticker, maturity, leg, amt in pos_rows:
        if leg is None:
            continue
        positions_by_wallet.setdefault(owner, []).append({
            "marketKey": mk,
            "ticker": ticker,
            "maturityDate": maturity,
            "leg": leg,
            "balance": float(amt or 0),
        })
    for owner, mk, ticker, maturity, bal, usd in clmm_lp_rows:
        positions_by_wallet.setdefault(owner, []).append({
            "marketKey": mk,
            "ticker": ticker,
            "maturityDate": maturity,
            "leg": "LP",
            "balance": float(bal or 0),
            "usdValue": float(usd or 0),
        })
    for ps in positions_by_wallet.values():
        ps.sort(key=lambda p: (-p["balance"], p["marketKey"]))

    # Ensure wallets with positions but no events still get a shard
    for wallet in positions_by_wallet:
        by_wallet.setdefault(wallet, [])

    # Restrict to wallets the UI links to so Vercel deploy stays well under
    # size limits. Wallets dropped here render the page's "no timeline data"
    # fallback when someone types the URL directly.
    if referenced:
        before = len(by_wallet)
        by_wallet = {w: e for w, e in by_wallet.items() if w in referenced}
        rprint(f"  filtered {before:,} → {len(by_wallet):,} (UI-referenced only)")

    rprint(f"  writing {len(by_wallet):,} wallet shards…")
    for wallet, events in by_wallet.items():
        with open(shard_dir / f"{wallet}.json", "w") as f:
            json.dump({
                "wallet": wallet,
                "events": events,
                "positions": positions_by_wallet.get(wallet, []),
            }, f, separators=(",", ":"))
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
            ("v2_orderbook.json", build_v2_orderbook_json),
            ("tranche.json", build_tranche_json),
            ("strategy_vault.json", build_strategy_vault_json),
        ]:
            rprint(f"[cyan]Building {name}…[/cyan]")
            payload = builder(con)
            _write_atomic(WEB_PUBLIC / name, payload)
            size = (WEB_PUBLIC / name).stat().st_size / 1024
            rprint(f"  wrote {name}  ({size:.1f} KB)")
        rprint("[cyan]Building strategy txn shards…[/cyan]")
        build_strategy_txn_shards(con)
        rprint("[cyan]Building wallet shards…[/cyan]")
        build_wallet_shards(con)
    finally:
        con.close()
    rprint("[green]serve done[/green]")
    # Run data-quality validation last — flag any anomalies that slipped
    # through. Non-blocking unless severity ≥ high (then exits non-zero).
    try:
        from serve.validate import main as validate_main
        rprint("\n[cyan]Running data-quality checks…[/cyan]")
        validate_main()
    except Exception as e:
        rprint(f"[yellow]warn[/yellow] validate step failed: {e}")


if __name__ == "__main__":
    build()
