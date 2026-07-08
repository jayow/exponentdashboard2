"""Decode strategy-vault EXECUTION activity — the manager's actual DeFi legs.

Every policy-gated execution a squads vault performs (Kamino deposit/borrow,
Titan swap, Exponent PT trade / SY mint, …) invokes the strategy-vault
program's validate_interaction_hook, so raw_strategy_vault_actions already
holds the signature of every execution tx. This extractor fetches those
transactions and distils each into one row:

  type   — verbs from instruction discriminators (K-Lend anchor discs via
           strategy_vault_proposal_decode.IX_DISC_VERBS; 1-byte shank ops
           for the Generic SY program; program-level labels otherwise)
  asset  — the squads vault's largest token balance delta in the tx
           (mint + ui amount, from meta pre/post token balances)
  programs — non-infra program ids invoked (protocol names resolve at serve
           time from the registry)

Incremental: signatures already present in raw_strategy_vault_executions
are skipped.

Usage:
    python -m extract_load.extract_strategy_vault_executions [--limit N]
"""
from __future__ import annotations
import argparse
import asyncio
import json
from datetime import datetime, timezone

import base58
from rich import print as rprint

from .config import RPC_ENDPOINTS
from .solana_rpc_client import SolanaRpcClient
from .load import warehouse
from .strategy_vault_proposal_decode import IX_DISC_VERBS

KLEND      = "KLend2g3cP87fffoy8q1mQqGKjrxjC8boSyAYavgmjD"
TITAN      = "T1TANpTeScyeqVzzgNViGDNrkQ6qHz9KrSBS4aNXvGT"
XPBOOK     = "XPBookgQTN2p8Yw1C2La35XkPMmZTCEYH77AdReVvK1"
XPCLMM     = "XPC1MM4dYACDfykNuXYZ5una2DsMDWL24CrYubCvarC"
CORE       = "ExponentnaRg3CQbW6dqQNZKXp7gtZ9DGMp1cwC4HAS7"
GENERIC_SY = "XP1BRLn8eCYSygrd8er5P4GKdzqKbC3DLoSsS5UYVZy"

# Programs that are plumbing, not strategy legs — excluded from the
# programs column so protocol chips stay meaningful.
_INFRA_PROGRAMS = {
    "sVau1tXvayVWfotzm9Ahcv2qfnnfRWttt78BCnNC6dD",
    "SQDS4ep65T869zMMBKyuUq6aD6EgTu8psMjkvj52pCf",
    "ComputeBudget111111111111111111111111111111",
    "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA",
    "TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb",
    "ATokenGPvbdGVxr1b2hvZbsiqW5xWH25efTNsLJA8knL",
    "11111111111111111111111111111111",
}

# Generic SY uses 1-byte shank opcodes.
_SY_OP_VERBS = {0x01: "Mint SY", 0x02: "Redeem SY", 0x05: "Deposit SY", 0x06: "Withdraw SY"}

_PROGRAM_VERBS = {
    TITAN:  "Swap",
    XPBOOK: "Trade PT",
    XPCLMM: "LP",
}


def _classify(tx: dict) -> tuple[list[str], list[str]]:
    """(verbs, program_ids) for one transaction."""
    msg = (tx.get("transaction") or {}).get("message") or {}
    all_ixs = list(msg.get("instructions") or [])
    for group in ((tx.get("meta") or {}).get("innerInstructions") or []):
        all_ixs.extend(group.get("instructions") or [])

    verbs: list[str] = []
    programs: list[str] = []

    def add(lst: list[str], v: str) -> None:
        if v not in lst:
            lst.append(v)

    for ix in all_ixs:
        pid = ix.get("programId")
        if not pid or pid in _INFRA_PROGRAMS:
            continue
        add(programs, pid)
        data_b58 = ix.get("data")
        raw = None
        if isinstance(data_b58, str):
            try:
                raw = base58.b58decode(data_b58)
            except ValueError:
                raw = None
        if pid == KLEND and raw and len(raw) >= 8:
            verb = IX_DISC_VERBS.get(raw[:8])
            if verb and verb not in ("Refresh", "Init user", "Init farms"):
                add(verbs, verb)
        elif pid == GENERIC_SY and raw:
            verb = _SY_OP_VERBS.get(raw[0])
            if verb:
                add(verbs, verb)
        elif pid in _PROGRAM_VERBS:
            add(verbs, _PROGRAM_VERBS[pid])
    return verbs, programs


def _squads_deltas(tx: dict, squads: str) -> list[dict]:
    """Per-mint UI balance change for token accounts owned by the squads
    vault, largest magnitude first."""
    meta = tx.get("meta") or {}
    per_mint: dict[str, float] = {}
    for sign, field in ((-1, "preTokenBalances"), (1, "postTokenBalances")):
        for b in meta.get(field) or []:
            if b.get("owner") != squads:
                continue
            ui = (b.get("uiTokenAmount") or {}).get("uiAmount")
            if ui is None:
                amt = (b.get("uiTokenAmount") or {}).get("amount")
                dec = (b.get("uiTokenAmount") or {}).get("decimals") or 0
                ui = int(amt or 0) / 10 ** dec
            per_mint[b["mint"]] = per_mint.get(b["mint"], 0.0) + sign * float(ui)
    deltas = [{"mint": m, "amountUi": round(v, 9)} for m, v in per_mint.items()
              if abs(v) > 1e-9]
    deltas.sort(key=lambda d: -abs(d["amountUi"]))
    return deltas


async def run(limit: int | None = None) -> dict:
    if not RPC_ENDPOINTS:
        raise RuntimeError("No SOLANA_RPC_URLS configured in .env")
    fetch_ts = datetime.now(timezone.utc)

    with warehouse() as con:
        squads_by_vault = dict(con.execute("""
            SELECT vault, squads_vault FROM raw_strategy_vault_states
            WHERE snapshot_date = (SELECT MAX(snapshot_date)
                                   FROM raw_strategy_vault_states)
        """).fetchall())
        # Self-heal: rows with no verbs AND no deltas are the signature of a
        # run where the squads mapping was unavailable (states extractor had
        # failed, e.g. RPC 429 on GHA) — drop them so they reprocess below.
        con.execute("""
            DELETE FROM raw_strategy_vault_executions
            WHERE (types IS NULL OR types = '')
              AND (deltas_json IS NULL OR deltas_json = '[]')
        """)
        work = con.execute("""
            SELECT DISTINCT a.signature, a.vault, a.block_time
            FROM raw_strategy_vault_actions a
            WHERE a.ix_name = 'validate_interaction_hook' AND a.tx_success
              AND a.signature NOT IN
                  (SELECT signature FROM raw_strategy_vault_executions)
            ORDER BY a.block_time
        """).fetchall()
    if limit:
        work = work[:limit]
    rprint(f"[cyan]Decoding {len(work)} execution tx(s)…[/cyan]")
    if not work:
        return {"executions": 0}

    rows = []
    CHUNK = 20  # keep the JSON-RPC batch under provider payload limits
    async with SolanaRpcClient(RPC_ENDPOINTS) as client:
        for i in range(0, len(work), CHUNK):
            chunk = work[i:i + CHUNK]
            txs = await client.get_transactions([w[0] for w in chunk])
            for (sig, vault, block_time), tx in zip(chunk, txs):
                if tx is None:
                    continue
                squads = squads_by_vault.get(vault)
                if not squads:
                    # No squads mapping (states snapshot missing this vault) —
                    # skip so the tx reprocesses once states are available.
                    continue
                verbs, programs = _classify(tx)
                deltas = _squads_deltas(tx, squads)
                primary = deltas[0] if deltas else None
                rows.append((
                    sig, vault, int(block_time or 0),
                    ",".join(verbs), ",".join(programs),
                    primary["mint"] if primary else None,
                    primary["amountUi"] if primary else None,
                    json.dumps(deltas, separators=(",", ":")),
                    fetch_ts,
                ))
            rprint(f"  {min(i + CHUNK, len(work))}/{len(work)}")

    with warehouse() as con:
        con.executemany(
            "INSERT OR IGNORE INTO raw_strategy_vault_executions VALUES "
            "(?, ?, ?, ?, ?, ?, ?, ?, ?)",
            rows,
        )
    rprint(f"Wrote {len(rows)} execution row(s)")
    return {"executions": len(rows)}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=None)
    args = ap.parse_args()
    started = datetime.now(timezone.utc)
    rprint(f"extract_strategy_vault_executions  started {started.isoformat()}")
    asyncio.run(run(limit=args.limit))
    rprint(f"done  elapsed {datetime.now(timezone.utc) - started}")


if __name__ == "__main__":
    main()
