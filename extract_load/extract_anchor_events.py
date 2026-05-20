"""Extract Anchor position-mutating events (YT/LP) from raw_helius_tx logs.

For every Exponent core / CLMM tx, scan logMessages in order. Pair each
`Program log: Instruction: <Name>` line with the most recent `Program <ID>
invoke [n]` line — that gives us (program_id, instruction_name) for every
inner instruction. Filter to position-mutating ones and write to
raw_anchor_position_events.

Each event is also tagged with a market_key by scanning the tx's
accountKeys for any pubkey that matches dim_markets identifying columns
(vault / amm_pool / pt_mint / yt_mint / lp_mint / sy_mint /
clmm_orderbook). This lets downstream models filter holders to
markets that were unmatured at any given historical date.

We do this in Python because the equivalent SQL (unnest + window) OOMs
DuckDB on the full 695k-tx history (35M+ log lines blow past 6 GiB temp).
Python streams batches and only retains ~150k matching rows.
"""
from __future__ import annotations
import json
import re
from collections import namedtuple
from rich import print as rprint

from .config import EXPONENT_CORE_PROGRAM, EXPONENT_CLMM_PROGRAM
from .load import warehouse


PROGRAMS = {EXPONENT_CORE_PROGRAM, EXPONENT_CLMM_PROGRAM}

# Mapping: instruction_name -> (leg, sign). Sign = +1 mints YT/LP, -1 burns,
# 0 init-only (no balance change in same op).
YT_LP_INSTRUCTIONS: dict[str, tuple[str, int]] = {
    # YT mint-side
    "Strip": ("YT", 1),
    "WrapperStrip": ("YT", 1),
    "BuyYt": ("YT", 1),
    "WrapperBuyYt": ("YT", 1),
    "DepositYt": ("YT", 1),
    # YT burn-side
    "Merge": ("YT", -1),
    "WrapperMerge": ("YT", -1),
    "SellYt": ("YT", -1),
    "WrapperSellYt": ("YT", -1),
    "WithdrawYt": ("YT", -1),
    # YT init
    "InitializeYieldPosition": ("YT", 0),
    # LP mint-side
    "MarketTwoDepositLiquidity": ("LP", 1),
    "MarketDepositLp": ("LP", 1),
    "WrapperProvideLiquidity": ("LP", 1),
    "WrapperProvideLiquidityBase": ("LP", 1),
    "DepositLiquidity": ("LP", 1),
    # LP burn-side
    "MarketTwoWithdrawLiquidity": ("LP", -1),
    "MarketWithdrawLp": ("LP", -1),
    "WrapperWithdrawLiquidity": ("LP", -1),
    "WrapperWithdrawLiquidityClassic": ("LP", -1),
    "WithdrawLiquidity": ("LP", -1),
    # LP init
    "InitLpPosition": ("LP", 0),
}

RE_INVOKE = re.compile(r"^Program ([A-Za-z0-9]{32,44}) invoke \[\d+\]$")
RE_IX = re.compile(r"^Program log: Instruction: (\w+)$")


def parse_logs(logs: list[str]) -> list[tuple[int, str, str]]:
    """Returns list of (log_index, program_id, instruction_name) for
    every Anchor instruction emitted by an Exponent program."""
    out: list[tuple[int, str, str]] = []
    # Stack of currently-active programs (mirrors CPI depth). The most
    # recent `invoke [n]` is the program for the next "Instruction:" log.
    stack: list[str] = []
    for i, line in enumerate(logs):
        m = RE_INVOKE.match(line)
        if m:
            stack.append(m.group(1))
            continue
        if line.endswith(" success") or "failed:" in line:
            # End of a program's frame. Pop, ignoring noisy text.
            if stack:
                stack.pop()
            continue
        m = RE_IX.match(line)
        if m and stack and stack[-1] in PROGRAMS:
            name = m.group(1)
            if name in YT_LP_INSTRUCTIONS:
                out.append((i, stack[-1], name))
    return out


def run() -> dict:
    rprint("[cyan]Extracting Anchor position events from raw_helius_tx…[/cyan]")
    n_in = 0
    rows: list[tuple] = []
    with warehouse() as con:
        # Target table — schema now includes market_key. If the column is
        # missing from a prior build, drop+recreate (cheap, fully derived).
        cols = {r[1] for r in con.execute("PRAGMA table_info('raw_anchor_position_events')").fetchall()}
        if cols and 'market_key' not in cols:
            con.execute("DROP TABLE raw_anchor_position_events")
        con.execute("""
            CREATE TABLE IF NOT EXISTS raw_anchor_position_events (
                signature        VARCHAR NOT NULL,
                block_time       BIGINT  NOT NULL,
                signer           VARCHAR,
                log_index        INTEGER NOT NULL,
                program_id       VARCHAR NOT NULL,
                instruction_name VARCHAR NOT NULL,
                leg              VARCHAR NOT NULL,
                action_sign      INTEGER NOT NULL,
                market_key       VARCHAR,
                PRIMARY KEY (signature, log_index)
            )
        """)
        # Full refresh — cheap (~150k rows max).
        con.execute("DELETE FROM raw_anchor_position_events")

        # Build pubkey -> market_key index from dim_markets. Each market has
        # several identifying pubkeys; any of them appearing in a tx's
        # accountKeys means the tx touched that market.
        rprint("  building market-pubkey index from dim_markets…")
        market_rows = con.execute("""
            SELECT market_key, vault, amm_pool, pt_mint, yt_mint, lp_mint, sy_mint, clmm_orderbook
            FROM main_core.dim_markets
        """).fetchall()
        pubkey_to_market: dict[str, str] = {}
        for mk, vault, amm, pt, yt, lp, sy, ob in market_rows:
            for pk in (vault, amm, pt, yt, lp, sy, ob):
                if pk and pk not in pubkey_to_market:
                    pubkey_to_market[pk] = mk
        # Also map CLMM market account → pt_mint → market_key via raw_clmm_markets.
        for mkt_acct, pt_mint in con.execute("""
            SELECT clmm_market, pt_mint FROM raw_clmm_markets
            WHERE snapshot_date = (SELECT MAX(snapshot_date) FROM raw_clmm_markets)
        """).fetchall():
            if pt_mint in pubkey_to_market and mkt_acct not in pubkey_to_market:
                pubkey_to_market[mkt_acct] = pubkey_to_market[pt_mint]
        rprint(f"  indexed {len(pubkey_to_market)} pubkeys → {len(market_rows)} markets")

        # Stream raw_helius_tx in chunks ordered by signature for stable progress.
        # Filter to txs that involve at least one of the Exponent programs
        # (via raw_signatures discovered_via). Cuts the input by ~half.
        cur = con.execute("""
            SELECT t.signature, t.block_time, t.payload
            FROM raw_helius_tx t
            WHERE t.signature IN (
                SELECT signature FROM raw_signatures
                WHERE discovered_via IN ('core_program', 'clmm_program', 'pool', 'vault')
            )
        """)
        while True:
            batch = cur.fetchmany(5000)
            if not batch:
                break
            for sig, bt, payload_json in batch:
                n_in += 1
                try:
                    p = json.loads(payload_json)
                    meta = p.get("meta") or {}
                    logs = meta.get("logMessages") or []
                    if not logs:
                        continue
                    parsed = parse_logs(logs)
                    if not parsed:
                        continue
                    msg = (p.get("transaction") or {}).get("message") or {}
                    account_keys = msg.get("accountKeys") or []
                    signer_addr = None
                    market_key = None
                    for i, ak in enumerate(account_keys):
                        pk = ak.get("pubkey") if isinstance(ak, dict) else ak
                        if i == 0:
                            signer_addr = pk
                        if market_key is None and pk in pubkey_to_market:
                            market_key = pubkey_to_market[pk]
                    for log_index, prog_id, ix_name in parsed:
                        leg, sign = YT_LP_INSTRUCTIONS[ix_name]
                        rows.append((sig, bt, signer_addr, log_index, prog_id, ix_name, leg, sign, market_key))
                except Exception:
                    pass
            if n_in % 50000 == 0:
                rprint(f"  scanned {n_in} txs, {len(rows)} matching events")

        rprint(f"[cyan]Parsed {n_in} txs → {len(rows)} matching events. Loading…[/cyan]")
        if rows:
            con.executemany(
                "INSERT INTO raw_anchor_position_events VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                rows,
            )
        # Action breakdown + market-attribution coverage
        n_attrib = con.execute(
            "SELECT COUNT(*), COUNT(market_key) FROM raw_anchor_position_events"
        ).fetchone()
        rprint(f"  events with market_key: {n_attrib[1]}/{n_attrib[0]} ({100*n_attrib[1]/n_attrib[0]:.1f}%)")
        counts = con.execute("""
            SELECT instruction_name, leg, action_sign, COUNT(*) FROM raw_anchor_position_events
            GROUP BY 1, 2, 3 ORDER BY 4 DESC
        """).fetchall()
        for ix, leg, sign, n in counts:
            rprint(f"  {ix:35s} {leg} {sign:+d}  {n}")

    return {"n_txs_scanned": n_in, "n_events": len(rows)}


def main() -> None:
    run()


if __name__ == "__main__":
    main()
