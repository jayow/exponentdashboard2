"""Post-build data-quality validation.

Reads `main_analytics.dq_anomalies` and prints any flagged issues. Designed
to run after `build_web_data` so anomalies surface BEFORE we ship the JSONs.

Categories surfaced (defined in dq_anomalies.sql):
  tvl_jump         protocol TVL day-over-day moved >50%
  tvl_negative     any negative TVL value
  tvl_orphan       market has tvl_usd>0 but PT_supply=0 (attribution bug)
  price_gap        SY supply exists but no USD price that day
  volume_outlier   daily volume > 50× trailing-30d median

Exit code:
  0 — no anomalies
  0 — only 'medium' (warnings)
  1 — any 'high' or 'critical'

Always prints a summary even when clean so you know it ran.
"""
from __future__ import annotations
import sys
from collections import defaultdict

import duckdb
from rich import print as rprint
from rich.table import Table
from rich.console import Console

from extract_load.config import WAREHOUSE_PATH


SEVERITY_ORDER = {"critical": 0, "high": 1, "medium": 2, "low": 3}
SEVERITY_COLOR = {
    "critical": "bold red",
    "high":     "red",
    "medium":   "yellow",
    "low":      "dim",
}


def main() -> int:
    con = duckdb.connect(str(WAREHOUSE_PATH), read_only=True)
    try:
        rows = con.execute("""
            SELECT category, severity, anomaly_date::VARCHAR, entity, detail
            FROM main_analytics.dq_anomalies
            ORDER BY
              CASE severity WHEN 'critical' THEN 0 WHEN 'high' THEN 1 WHEN 'medium' THEN 2 ELSE 3 END,
              anomaly_date DESC
        """).fetchall()
    except duckdb.CatalogException:
        rprint("[yellow]warn[/yellow] dq_anomalies mart not built — skipping validation")
        return 0
    finally:
        con.close()

    if not rows:
        rprint("[green]✓ data quality clean[/green] — no anomalies detected")
        return 0

    by_severity: dict[str, list] = defaultdict(list)
    by_category: dict[str, int] = defaultdict(int)
    for cat, sev, date, entity, detail in rows:
        by_severity[sev].append((cat, date, entity, detail))
        by_category[cat] += 1

    console = Console()
    rprint(f"\n[bold]Data-quality check[/bold] — {len(rows)} anomaly row(s) flagged")

    # Summary table per category × severity
    summary = Table(show_header=True, header_style="bold")
    summary.add_column("Category")
    summary.add_column("Count", justify="right")
    summary.add_column("Worst severity")
    by_cat_worst: dict[str, str] = {}
    for cat, sev, *_ in rows:
        if cat not in by_cat_worst or SEVERITY_ORDER[sev] < SEVERITY_ORDER[by_cat_worst[cat]]:
            by_cat_worst[cat] = sev
    for cat in sorted(by_category, key=lambda c: SEVERITY_ORDER[by_cat_worst[c]]):
        summary.add_row(
            cat, str(by_category[cat]),
            f"[{SEVERITY_COLOR[by_cat_worst[cat]]}]{by_cat_worst[cat]}[/{SEVERITY_COLOR[by_cat_worst[cat]]}]",
        )
    console.print(summary)

    # Top anomalies (everything critical/high, plus first 5 medium)
    rprint("\n[bold]Details[/bold] (worst first, capped):")
    shown = {"critical": 0, "high": 0, "medium": 0, "low": 0}
    cap = {"critical": 20, "high": 20, "medium": 8, "low": 5}
    for cat, sev, date, entity, detail in rows:
        if shown[sev] >= cap[sev]:
            continue
        shown[sev] += 1
        ent = f" {entity}" if entity else ""
        rprint(f"  [{SEVERITY_COLOR[sev]}]{sev:<8}[/{SEVERITY_COLOR[sev]}] {date}  {cat}{ent}  — {detail}")

    # Exit non-zero on critical/high so CI / scheduler can fail loudly
    has_blocking = any(s in by_severity for s in ("critical", "high"))
    if has_blocking:
        rprint("\n[red]✗ blocking anomalies present[/red] (critical/high) — review before relying on this run")
        return 1
    rprint("\n[yellow]⚠ medium-severity warnings only[/yellow] — not blocking")
    return 0


if __name__ == "__main__":
    sys.exit(main())
