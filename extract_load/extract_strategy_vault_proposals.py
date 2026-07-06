"""Snapshot ActionProposal accounts (strategy-vault governance).

getProgramAccounts sweep for ActionProposal (disc 90c0c2d2f1adf029) under
EXPONENT_STRATEGY_VAULT_PROGRAM, decoded via the program IDL
(strategy_vault_proposal_decode). One row per proposal with header fields
plus a structural per-action summary (kind, mode, referenced pubkeys,
permitted-instruction verbs); pubkey→symbol/protocol resolution happens at
serve time where token metadata lives.

Proposals are lifecycle accounts (Draft→Active→Executed/Cancelled/Rejected),
so each run replaces the whole table with the current on-chain state.
Accounts whose action_data uses an older layout decode header-only with
parse_error set — display code should degrade gracefully.

Usage:
    python -m extract_load.extract_strategy_vault_proposals
"""
from __future__ import annotations
import asyncio
import base64
import json
from datetime import datetime, timezone

import base58
from rich import print as rprint

from .config import RPC_ENDPOINTS, EXPONENT_STRATEGY_VAULT_PROGRAM
from .solana_rpc_client import SolanaRpcClient
from .load import warehouse
from .strategy_vault_proposal_decode import (
    ACTION_PROPOSAL_DISCRIMINATOR,
    decode_action_proposal,
    summarize_action,
)


async def run() -> dict:
    if not RPC_ENDPOINTS:
        raise RuntimeError("No SOLANA_RPC_URLS configured in .env")

    fetch_ts = datetime.now(timezone.utc)
    disc_b58 = base58.b58encode(ACTION_PROPOSAL_DISCRIMINATOR).decode()

    async with SolanaRpcClient(RPC_ENDPOINTS) as client:
        accts = await client.get_program_accounts(
            EXPONENT_STRATEGY_VAULT_PROGRAM,
            filters=[{"memcmp": {"offset": 0, "bytes": disc_b58}}],
        )
    rprint(f"[cyan]Fetched {len(accts)} ActionProposal accounts[/cyan]")

    rows, ok, partial = [], 0, 0
    for a in accts:
        data_field = a["account"]["data"]
        data_b64 = data_field[0] if isinstance(data_field, list) else data_field
        raw = base64.b64decode(data_b64)
        try:
            p = decode_action_proposal(raw)
            summaries = [summarize_action(act) for act in p["actions"]]
            parse_error = None
            ok += 1
        except Exception as e:
            # Header layout is stable; action_data of some older proposals
            # uses a different framing. Fall back to header-only.
            p = _header_only(raw)
            if p is None:
                rprint(f"  [yellow]warn[/yellow] {a['pubkey'][:8]}… undecodable: {e}")
                continue
            summaries = []
            parse_error = str(e)[:200]
            partial += 1
        rows.append((
            a["pubkey"], p["vault"], p["proposal_id"], p["proposer"],
            p["status"], p["created_at"], p["voting_ends_at"],
            p["timelock_seconds"], p["executable_at"],
            p["reject_votes"], p["opt_out_votes"], p["lp_supply_snapshot"],
            len(summaries),
            json.dumps(summaries, separators=(",", ":")),
            parse_error, fetch_ts,
        ))

    with warehouse() as con:
        con.execute("DELETE FROM raw_strategy_vault_proposals")
        if rows:
            con.executemany(
                "INSERT INTO raw_strategy_vault_proposals VALUES "
                "(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                rows,
            )

    rprint(f"Wrote {len(rows)} proposal row(s)  (full={ok} header-only={partial})")
    return {"proposals": len(rows), "full": ok, "header_only": partial}


def _header_only(raw: bytes) -> dict | None:
    """Best-effort header decode when action_data parsing fails."""
    import struct
    from .strategy_vault_proposal_decode import PROPOSAL_STATUS
    try:
        off = 8
        vault = base58.b58encode(raw[off:off + 32]).decode(); off += 32
        pid = struct.unpack_from("<Q", raw, off)[0]; off += 8
        proposer = base58.b58encode(raw[off:off + 32]).decode(); off += 32
        dlen = struct.unpack_from("<I", raw, off)[0]; off += 4 + dlen
        created, ends = struct.unpack_from("<qq", raw, off); off += 16
        timelock = struct.unpack_from("<I", raw, off)[0]; off += 4
        executable = struct.unpack_from("<q", raw, off)[0]; off += 8
        status = raw[off]; off += 1
        reject, optout = struct.unpack_from("<QQ", raw, off); off += 16
        off += 1  # bump
        lp_snap = struct.unpack_from("<Q", raw, off)[0]
        return {
            "vault": vault, "proposal_id": pid, "proposer": proposer,
            "status": PROPOSAL_STATUS[status] if status < len(PROPOSAL_STATUS) else str(status),
            "created_at": created, "voting_ends_at": ends,
            "timelock_seconds": timelock, "executable_at": executable,
            "reject_votes": reject, "opt_out_votes": optout,
            "lp_supply_snapshot": lp_snap,
        }
    except Exception:
        return None


def main() -> None:
    started = datetime.now(timezone.utc)
    rprint(f"extract_strategy_vault_proposals  started {started.isoformat()}")
    asyncio.run(run())
    rprint(f"done  elapsed {datetime.now(timezone.utc) - started}")


if __name__ == "__main__":
    main()
