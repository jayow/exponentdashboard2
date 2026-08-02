"""Fetch Exponent's managed-strategy registry (names, APYs, metadata).

GET https://api.exponent.finance/strategies/managed — the same source the
Exponent frontend renders on /en/strategy/<address>?type=managed. Gives the
curated display name, strategist, deposit/quote tokens, capacity, fees, and
Exponent's own windowed APYs computed from their lpPrice history.

One row per (address, fetch_date); same-day re-runs overwrite (idempotent).
The full payload is kept in payload_json for fields we don't type out.

Usage:
    python -m extract_load.extract_strategy_vault_registry
"""
from __future__ import annotations
import json
from datetime import datetime, timezone

import httpx
from rich import print as rprint

from .load import warehouse


REGISTRY_URL = "https://api.exponent.finance/strategies/managed"


def fetch_registry(timeout: float = 30.0) -> list[dict]:
    resp = httpx.get(REGISTRY_URL, timeout=timeout)
    resp.raise_for_status()
    data = resp.json()
    if not isinstance(data, list):
        raise ValueError(f"unexpected registry payload type: {type(data)}")
    return data


def run() -> dict:
    fetch_ts = datetime.now(timezone.utc)
    fetch_date = fetch_ts.date()

    strategies = fetch_registry()
    rprint(f"[cyan]Fetched {len(strategies)} managed strategies from Exponent registry[/cyan]")

    rows = []
    for s in strategies:
        apy = s.get("apy") or {}
        dep = s.get("depositToken") or {}
        quote = s.get("quoteToken") or {}
        rows.append((
            fetch_date,
            s.get("address"),
            s.get("name"),
            s.get("strategistName"),
            s.get("profile"),
            s.get("description"),
            dep.get("mint"),
            dep.get("ticker"),
            quote.get("ticker"),
            apy.get("3d"),
            apy.get("7d"),
            apy.get("30d"),
            apy.get("current"),
            apy.get("ptWeight"),
            s.get("lpPrice"),
            s.get("tvl"),
            s.get("exchangeRate"),
            s.get("capacity"),
            s.get("managementFee"),
            s.get("performanceFee"),
            json.dumps(s, separators=(",", ":")),
            fetch_ts,
        ))

    with warehouse() as con:
        con.execute(
            "DELETE FROM raw_strategy_vault_registry WHERE fetch_date = ?",
            [fetch_date],
        )
        if rows:
            con.executemany(
                "INSERT INTO raw_strategy_vault_registry VALUES "
                "(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                rows,
            )

    rprint(f"Wrote {len(rows)} registry row(s)")
    return {"strategies": len(rows)}


def main() -> None:
    started = datetime.now(timezone.utc)
    rprint(f"extract_strategy_vault_registry  started {started.isoformat()}")
    run()
    rprint(f"done  elapsed {datetime.now(timezone.utc) - started}")


if __name__ == "__main__":
    main()
