"""Query DuckDB marts and emit slim per-tab JSONs into web/public/.

Replaces v1's monolithic analytics.json with one file per dashboard tab:
  overview.json, volume.json, markets.json, holders.json, fees.json, positions.json

Each is lazy-loaded by the frontend, keeping initial payload small.

Stub — Phase 4 implements after marts have real data.
"""
from __future__ import annotations
import json
from pathlib import Path

import duckdb

from extract_load.config import WAREHOUSE_PATH, ROOT
from . import queries

WEB_PUBLIC = ROOT / "web" / "public"


def build() -> None:
    WEB_PUBLIC.mkdir(parents=True, exist_ok=True)
    con = duckdb.connect(str(WAREHOUSE_PATH), read_only=True)
    try:
        # Phase 4: actually query + transpose into time-series shape.
        # placeholder so the file is runnable without crashing.
        _ = con.execute("select 1").fetchall()
        (WEB_PUBLIC / "overview.json").write_text(json.dumps({"status": "scaffold"}))
        print(f"serve: wrote scaffold JSONs into {WEB_PUBLIC}")
    finally:
        con.close()


if __name__ == "__main__":
    build()
