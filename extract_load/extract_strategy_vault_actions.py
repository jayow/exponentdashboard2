"""Decode per-instruction actions against Exponent Strategy Vaults.

Walks unprocessed signatures from raw_strategy_vault_signatures and emits
one raw_strategy_vault_actions row per top-level or inner instruction that
targets the Strategy Vault program (sVau1tXv…). Discriminator match against
IX_DISCRIMINATORS in strategy_vault_decode; unknown → name='unknown' but
we still store the discriminator hex for later triage.

INSTRUCTIONS are 1-byte shank-style opcodes (NOT 8-byte Anchor sha256). The
disc slice below is data_bytes[:1], and the arg payload starts at data[1:].
See docs/STRATEGY_VAULTS_PROBE.md section 8 for the correction rationale.

Inner ixs whose first 8 bytes equal ANCHOR_EMIT_CPI_MARKER
(e445a52e51cb9a1d) are Anchor `emit_cpi!` event emissions — the program
self-CPIs to itself just to log an event. We tag these as
'anchor_event_cpi' and skip arg parsing (the payload is an event struct,
not an instruction arg tail).

Recognised instructions (38): initialize_vault, deposit_liquidity,
add_policy, remove_policy, queue_withdrawal, fill_withdrawal,
execute_withdrawal, update_price, manager_update_position,
sentinel_set_vault_flags, stake_vote, finalize_proposal, unstake_vote,
cancel_proposal, execute_proposal, update_policy, wrapper_add_policy,
wrapper_remove_policy, wrapper_update_policy, validate_interaction_hook,
manage_vault_settings, initialize_prices, manage_prices, update_twap_price,
cancel_withdrawal, execute_withdrawal_from_reserves,
wrapper_execute_withdrawal, collect_management_fee, make_sentinel_manager,
update_policy_manager, init_proposal, append_proposal_actions,
activate_proposal, sync_policy_authorities, migrate_vault_layout,
refresh_aum, unblock_nav_aum_circuit_breaker, initialize_lp_metadata.

Arg parser supports primitive types (u8, u16, u32, u64, i64, bool, pubkey).
For instructions carrying structs / enums / vecs / options / strings we
store {'raw_hex': '<payload hex>'} under args_json and log a warning.

Deltas (lp_delta, base_delta) are computed from meta.preTokenBalances /
meta.postTokenBalances by matching the vault's mint_lp / underlying_mint
(joined from the latest raw_strategy_vault_states row). Positive = tokens
moved INTO the vault's balance across the tx.

Program return payload (base64-decoded → hex) is captured from any log line
matching 'Program return: sVau1t…'; useful for reconstructing withdrawal
amounts emitted by execute_withdrawal[_from_reserves].
"""
from __future__ import annotations
import argparse
import asyncio
import base64
import json
import logging
import struct
from datetime import datetime, timezone

import base58
from rich import print as rprint

from .config import RPC_ENDPOINTS, EXPONENT_STRATEGY_VAULT_PROGRAM, EXTRACT_BATCH_SIZE
from .solana_rpc_client import SolanaRpcClient
from .load import warehouse
from .strategy_vault_decode import (
    ANCHOR_EMIT_CPI_MARKER,
    IX_ARGS,
    IX_DISCRIMINATORS,
)

_log = logging.getLogger(__name__)

_PRIMITIVE_TYPES = {"u8", "u16", "u32", "u64", "i64", "bool", "pubkey"}


# ---------- ix data decoding ----------

def _decode_ix_data(data_field) -> bytes | None:
    """jsonParsed getTransaction returns ix.data as a base58 str for programs
    without a first-party parser (our case). Handle both base58 and the
    ["...", "base64"] tuple form defensively — Helius sometimes returns the
    latter for inner ixs on complex CPIs.
    """
    if data_field is None:
        return None
    if isinstance(data_field, list):
        # [payload, encoding] form
        payload, enc = data_field[0], (data_field[1] if len(data_field) > 1 else "base58")
        try:
            if enc == "base64":
                return base64.b64decode(payload)
            return base58.b58decode(payload)
        except Exception:
            return None
    if isinstance(data_field, str):
        # Try base58 first (Anchor default in jsonParsed), fall back to base64
        try:
            return base58.b58decode(data_field)
        except Exception:
            try:
                return base64.b64decode(data_field)
            except Exception:
                return None
    return None


def _read_primitive(payload: bytes, off: int, ty: str) -> tuple[object, int]:
    """Read one primitive Borsh value. Raises struct.error on short buffer."""
    if ty == "u8":
        return payload[off], off + 1
    if ty == "u16":
        return struct.unpack_from("<H", payload, off)[0], off + 2
    if ty == "u32":
        return struct.unpack_from("<I", payload, off)[0], off + 4
    if ty == "u64":
        return struct.unpack_from("<Q", payload, off)[0], off + 8
    if ty == "i64":
        return struct.unpack_from("<q", payload, off)[0], off + 8
    if ty == "bool":
        return bool(payload[off]), off + 1
    if ty == "pubkey":
        return base58.b58encode(payload[off:off + 32]).decode(), off + 32
    raise ValueError(f"not a primitive type: {ty}")


def _parse_args(ix_name: str, data: bytes) -> dict | None:
    """Parse the arg tail (after the 1-byte shank discriminator) per IDL.

    - Empty arg list → {}.
    - All-primitive arg list → decoded dict.
    - Contains any non-primitive (struct / enum / vec / option / string) →
      {'raw_hex': '<payload>'} plus a warning log so callers can spot which
      ix names we still need structured parsers for.
    - Returns None only when the arg spec is unknown (ix_name not in IX_ARGS).
    """
    spec = IX_ARGS.get(ix_name)
    if spec is None:
        return None

    payload = data[1:]

    if not spec:
        return {}

    if not all(ty in _PRIMITIVE_TYPES for _, ty in spec):
        _log.warning(
            "ix %s has non-primitive args %s; storing raw hex payload",
            ix_name,
            [(n, t) for n, t in spec if t not in _PRIMITIVE_TYPES],
        )
        return {"raw_hex": payload.hex()}

    try:
        out: dict[str, object] = {}
        off = 0
        for name, ty in spec:
            value, off = _read_primitive(payload, off, ty)
            out[name] = value
        return out
    except (struct.error, IndexError):
        return {"raw_hex": payload.hex()}


# ---------- account index → pubkey resolution ----------

def _resolve_account_keys(tx: dict) -> list[str]:
    """Return the full account-key list for a tx, INCLUDING loaded addresses
    from lookup tables (writable + readonly, in that order — matches Solana
    runtime ordering). jsonParsed returns each entry as {'pubkey', 'signer',
    'writable', 'source'} or a bare str for versioned tx variants."""
    msg = (tx.get("transaction") or {}).get("message") or {}
    keys_raw = msg.get("accountKeys") or []
    keys: list[str] = []
    for k in keys_raw:
        if isinstance(k, dict):
            keys.append(k.get("pubkey"))
        else:
            keys.append(k)
    loaded = (tx.get("meta") or {}).get("loadedAddresses") or {}
    for k in loaded.get("writable") or []:
        keys.append(k)
    for k in loaded.get("readonly") or []:
        keys.append(k)
    return keys


def _extract_vault_from_accounts(ix_name: str, account_pks: list[str],
                                  vault_lookup: dict[str, dict]) -> str | None:
    """Return the pubkey from the ix's account list that matches a known
    strategy vault. Per IDL, vault is at:
      accounts[1] for deposit_liquidity / queue_withdrawal / execute_withdrawal_from_reserves
                     / refresh_aum → wait, refresh_aum has vault at [0]
      accounts[0] for refresh_aum / execute_withdrawal
      accounts[1] for collect_management_fee / manager_update_position
    Rather than encode a per-ix map (fragile), just find the first account_pk
    that appears in vault_lookup. This is robust to IDL drift."""
    for pk in account_pks:
        if pk in vault_lookup:
            return pk
    return None


# ---------- balance deltas ----------

def _vault_owned_delta(
    pre_balances: list[dict],
    post_balances: list[dict],
    mint: str | None,
    vault_pk: str,
) -> int:
    """Sum delta on any token account owned by `vault_pk` for `mint`.
    Returns 0 if the mint is None or no matching entries."""
    if not mint:
        return 0
    pre = {b["accountIndex"]: b for b in pre_balances}
    post = {b["accountIndex"]: b for b in post_balances}
    total = 0
    for idx in sorted(set(pre) | set(post)):
        a, b = pre.get(idx, {}), post.get(idx, {})
        m = b.get("mint") or a.get("mint")
        owner = b.get("owner") or a.get("owner")
        if m != mint or owner != vault_pk:
            continue
        pre_amt = int((a.get("uiTokenAmount") or {}).get("amount", "0") or "0")
        post_amt = int((b.get("uiTokenAmount") or {}).get("amount", "0") or "0")
        total += (post_amt - pre_amt)
    return total


def _mint_supply_delta(
    pre_balances: list[dict],
    post_balances: list[dict],
    mint: str | None,
) -> int:
    """Sum ALL wallet-owned deltas for `mint` — approximates SPL mint supply
    change over the tx (positive = LP minted, negative = LP burned). Used for
    lp_delta since LP tokens live in the user's ATA, not the vault's."""
    if not mint:
        return 0
    pre = {b["accountIndex"]: b for b in pre_balances}
    post = {b["accountIndex"]: b for b in post_balances}
    total = 0
    for idx in sorted(set(pre) | set(post)):
        a, b = pre.get(idx, {}), post.get(idx, {})
        m = b.get("mint") or a.get("mint")
        if m != mint:
            continue
        pre_amt = int((a.get("uiTokenAmount") or {}).get("amount", "0") or "0")
        post_amt = int((b.get("uiTokenAmount") or {}).get("amount", "0") or "0")
        total += (post_amt - pre_amt)
    return total


# ---------- program return log parsing ----------

_PROGRAM_RETURN_PREFIX = f"Program return: {EXPONENT_STRATEGY_VAULT_PROGRAM} "


def _extract_program_return_hex(logs: list[str]) -> str | None:
    for line in logs or []:
        if line.startswith(_PROGRAM_RETURN_PREFIX):
            payload_b64 = line[len(_PROGRAM_RETURN_PREFIX):].strip()
            if not payload_b64:
                return None
            try:
                return base64.b64decode(payload_b64).hex()
            except Exception:
                return None
    return None


# ---------- per-instruction walk ----------

def _iter_program_instructions(tx: dict, account_keys: list[str]):
    """Yield (ix_index, ix_dict) for every top-level + inner ix targeting the
    Strategy Vault program. ix_index is a monotonically increasing counter
    across top-level (0..N) then inner (N..M) so it's stable per-tx."""
    msg = (tx.get("transaction") or {}).get("message") or {}
    top_ixs = msg.get("instructions") or []
    counter = 0
    for ix in top_ixs:
        pid = ix.get("programId")
        if pid == EXPONENT_STRATEGY_VAULT_PROGRAM:
            yield counter, ix
        counter += 1

    inner_groups = ((tx.get("meta") or {}).get("innerInstructions") or [])
    for group in inner_groups:
        for ix in group.get("instructions") or []:
            pid = ix.get("programId")
            if pid == EXPONENT_STRATEGY_VAULT_PROGRAM:
                yield counter, ix
            counter += 1


def _ix_account_pks(ix: dict, account_keys: list[str]) -> list[str]:
    """jsonParsed encoding stores accounts as an array of pubkey strings for
    non-parsed programs; for parsed programs it can be a struct. We fall back
    to resolving numeric indices via account_keys just in case."""
    accs = ix.get("accounts") or []
    out: list[str] = []
    for a in accs:
        if isinstance(a, str):
            out.append(a)
        elif isinstance(a, int) and 0 <= a < len(account_keys):
            out.append(account_keys[a])
    return out


# ---------- main runner ----------

async def run(limit: int | None = None) -> dict:
    if not RPC_ENDPOINTS:
        raise RuntimeError("No SOLANA_RPC_URLS configured in .env")

    started = datetime.now(timezone.utc)
    rprint(f"[cyan]extract_strategy_vault_actions  started {started.isoformat()}[/cyan]")

    # 1. Find unprocessed signatures (LEFT JOIN pattern).
    # 2. Build a vault→(mint_lp, underlying_mint) lookup from the latest snapshot
    #    per vault in raw_strategy_vault_states. We use MAX(snapshot_date) per
    #    vault so newly-created vaults still resolve even if today's snapshot
    #    hasn't fired yet.
    with warehouse() as con:
        limit_sql = f"LIMIT {int(limit)}" if limit else ""
        sig_rows = con.execute(
            f"""
            SELECT s.signature, s.slot, s.block_time, s.err
              FROM raw_strategy_vault_signatures s
              LEFT JOIN raw_strategy_vault_actions a ON a.signature = s.signature
             WHERE a.signature IS NULL
             ORDER BY s.block_time DESC NULLS LAST
             {limit_sql}
            """
        ).fetchall()

        vault_rows = con.execute(
            """
            WITH latest AS (
                SELECT vault, MAX(snapshot_date) AS d
                  FROM raw_strategy_vault_states
                 GROUP BY vault
            )
            SELECT s.vault, s.mint_lp, s.underlying_mint
              FROM raw_strategy_vault_states s
              JOIN latest l ON l.vault = s.vault AND l.d = s.snapshot_date
            """
        ).fetchall()

    vault_lookup: dict[str, dict] = {
        v: {"mint_lp": lp, "underlying_mint": u} for v, lp, u in vault_rows
    }

    if not sig_rows:
        rprint("  [yellow]no unprocessed signatures[/yellow]")
        return {"signatures": 0, "rows": 0}
    if not vault_lookup:
        rprint("  [yellow]no strategy vault states yet — run extract_strategy_vault_states first[/yellow]")
        return {"signatures": len(sig_rows), "rows": 0}

    rprint(f"  {len(sig_rows)} unprocessed sig(s); {len(vault_lookup)} known vault(s)")

    sig_list = [r[0] for r in sig_rows]
    sig_meta_by_sig = {r[0]: {"slot": r[1], "block_time": r[2], "err": r[3]} for r in sig_rows}

    all_rows: list[tuple] = []
    async with SolanaRpcClient(RPC_ENDPOINTS) as client:
        for i in range(0, len(sig_list), EXTRACT_BATCH_SIZE):
            chunk = sig_list[i : i + EXTRACT_BATCH_SIZE]
            txs = await client.get_transactions(chunk)
            for sig, tx in zip(chunk, txs):
                if tx is None:
                    continue
                rows = _decode_tx(sig, tx, sig_meta_by_sig[sig], vault_lookup)
                all_rows.extend(rows)

    rprint(f"  decoded {len(all_rows)} action row(s) from {len(sig_list)} sig(s)")

    if all_rows:
        with warehouse() as con:
            con.executemany(
                """INSERT INTO raw_strategy_vault_actions (
                    signature, ix_index, slot, block_time, vault, ix_name,
                    ix_discriminator_hex, actor, lp_delta, base_delta,
                    program_return_hex, args_json, tx_success
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT (signature, ix_index) DO NOTHING""",
                all_rows,
            )

    rprint(f"[green]wrote {len(all_rows)} action row(s)[/green]  elapsed {datetime.now(timezone.utc) - started}")
    return {"signatures": len(sig_list), "rows": len(all_rows)}


def _decode_tx(sig: str, tx: dict, sig_meta: dict, vault_lookup: dict[str, dict]) -> list[tuple]:
    """Return raw_strategy_vault_actions rows for one tx."""
    meta = tx.get("meta") or {}
    tx_success = meta.get("err") is None
    logs = meta.get("logMessages") or []
    program_return_hex = _extract_program_return_hex(logs)
    pre_balances = meta.get("preTokenBalances") or []
    post_balances = meta.get("postTokenBalances") or []

    account_keys = _resolve_account_keys(tx)
    actor = account_keys[0] if account_keys else None

    slot = tx.get("slot") or sig_meta.get("slot")
    block_time = tx.get("blockTime") or sig_meta.get("block_time")

    rows: list[tuple] = []
    for ix_index, ix in _iter_program_instructions(tx, account_keys):
        data_bytes = _decode_ix_data(ix.get("data"))
        if data_bytes is None or len(data_bytes) < 1:
            # Cannot classify — skip. (This should be vanishingly rare given
            # the ix targets our program; if it happens, we lack the disc.)
            continue

        # Anchor emit_cpi self-CPI: first 8 bytes are the sha256("anchor:event")
        # sentinel, then an 8-byte event disc, then the event struct. This is
        # NOT a real instruction — tag & skip arg parsing.
        if len(data_bytes) >= 8 and data_bytes[:8] == ANCHOR_EMIT_CPI_MARKER:
            ix_name = "anchor_event_cpi"
            disc_hex = ANCHOR_EMIT_CPI_MARKER.hex()
            args_json = None
        else:
            disc = data_bytes[:1]
            disc_hex = disc.hex()
            ix_name = IX_DISCRIMINATORS.get(disc, "unknown")
            args = _parse_args(ix_name, data_bytes) if ix_name != "unknown" else None
            args_json = json.dumps(args) if args is not None else None

        ix_account_pks = _ix_account_pks(ix, account_keys)
        vault_pk = _extract_vault_from_accounts(ix_name, ix_account_pks, vault_lookup)
        if vault_pk is None:
            # No known vault referenced by this ix — could be an init or a
            # newly-created vault we haven't snapshotted yet. Still record it
            # so we don't reprocess the sig; leave vault/lp_delta/base_delta null.
            rows.append((
                sig, ix_index, slot, block_time, None, ix_name, disc_hex,
                actor, None, None, program_return_hex, args_json, tx_success,
            ))
            continue

        mints = vault_lookup[vault_pk]
        lp_delta = _mint_supply_delta(pre_balances, post_balances, mints.get("mint_lp"))
        base_delta = _vault_owned_delta(pre_balances, post_balances,
                                        mints.get("underlying_mint"), vault_pk)

        rows.append((
            sig, ix_index, slot, block_time, vault_pk, ix_name, disc_hex,
            actor, lp_delta, base_delta, program_return_hex, args_json, tx_success,
        ))
    return rows


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--limit", type=int, default=None,
                   help="Only process the N most-recent unprocessed signatures.")
    args = p.parse_args()
    asyncio.run(run(limit=args.limit))


if __name__ == "__main__":
    main()
