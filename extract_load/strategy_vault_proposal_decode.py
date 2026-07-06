"""Decode ActionProposal accounts (vault governance) via the program IDL.

Two layers:
  1. A generic borsh decoder driven by idls/exponent_strategy_vault.json —
     walks IDL type definitions (struct/enum/vec/option/array/primitives),
     so nested trees like ProposalAction → PolicyAction → PolicyConfig →
     ProgramInteractionPolicyCreationPayload → InstructionConstraint decode
     precisely without hand-written per-struct readers.
  2. A summarizer that flattens a decoded ProposalAction into the display
     row the UI needs: action name, mode, protocol (program_id → name),
     referenced mints (→ symbols upstream), and permitted-instruction verbs
     (anchor discriminators inside DataValue::U8Slice constraints, matched
     against known K-Lend instruction names).

ActionProposal account header layout follows the IDL struct; action_data is
a `bytes` field containing back-to-back serialized ProposalAction entries
(one per append_proposal_actions element).
"""
from __future__ import annotations
import hashlib
import json
import struct
from pathlib import Path
from typing import Any

import base58


IDL_PATH = Path(__file__).resolve().parent.parent / "idls" / "exponent_strategy_vault.json"

ACTION_PROPOSAL_DISCRIMINATOR = bytes([144, 192, 194, 210, 241, 173, 240, 41])

PROPOSAL_STATUS = ["Active", "Approved", "Rejected", "Executed", "Cancelled", "Draft"]

# Anchor instruction discriminators → human verb, for the protocols vault
# policies commonly whitelist. Unknown discriminators fall back to hex.
_KAMINO_IX_VERBS = {
    "deposit_reserve_liquidity_and_obligation_collateral": "Deposit",
    "deposit_reserve_liquidity_and_obligation_collateral_v2": "Deposit",
    "deposit_reserve_liquidity":                           "Deposit",
    "deposit_obligation_collateral":                       "Deposit",
    "deposit_obligation_collateral_v2":                    "Deposit",
    "borrow_obligation_liquidity":                         "Borrow",
    "borrow_obligation_liquidity_v2":                      "Borrow",
    "repay_obligation_liquidity":                          "Repay",
    "repay_obligation_liquidity_v2":                       "Repay",
    "repay_and_withdraw_and_redeem":                       "Repay + Withdraw",
    "deposit_and_withdraw":                                "Deposit + Withdraw",
    "withdraw_obligation_collateral_and_redeem_reserve_collateral": "Withdraw",
    "withdraw_obligation_collateral_and_redeem_reserve_collateral_v2": "Withdraw",
    "withdraw_obligation_collateral":                      "Withdraw",
    "withdraw_obligation_collateral_v2":                   "Withdraw",
    "redeem_reserve_collateral":                           "Withdraw",
    "redeem_reserve_collateral_v2":                        "Withdraw",
    "flash_borrow_reserve_liquidity":                      "Flash borrow",
    "flash_repay_reserve_liquidity":                       "Flash repay",
    "init_obligation":                                     "Init obligation",
    "init_obligation_farms_for_reserve":                   "Init farms",
    "init_user_metadata":                                  "Init user",
    "refresh_reserve":                                     "Refresh",
    "refresh_obligation":                                  "Refresh",
    "refresh_obligation_farms_for_reserve":                "Refresh",
    "request_elevation_group":                             "Elevation group",
}
IX_DISC_VERBS: dict[bytes, str] = {
    hashlib.sha256(f"global:{name}".encode()).digest()[:8]: verb
    for name, verb in _KAMINO_IX_VERBS.items()
}

_PRIMITIVE_SIZES = {
    "bool": 1, "u8": 1, "i8": 1, "u16": 2, "i16": 2, "u32": 4, "i32": 4,
    "u64": 8, "i64": 8, "u128": 16, "i128": 16, "f32": 4, "f64": 8,
}


class IdlDecoder:
    """Generic borsh decoder over an Anchor IDL's `types` table."""

    def __init__(self, idl: dict):
        self.types = {t["name"]: t["type"] for t in idl.get("types", [])}

    def decode(self, tref: Any, buf: bytes, off: int) -> tuple[Any, int]:
        if isinstance(tref, str):
            return self._decode_primitive(tref, buf, off)
        if "defined" in tref:
            name = tref["defined"]["name"] if isinstance(tref["defined"], dict) else tref["defined"]
            return self._decode_defined(name, buf, off)
        if "vec" in tref:
            n = struct.unpack_from("<I", buf, off)[0]
            off += 4
            out = []
            for _ in range(n):
                v, off = self.decode(tref["vec"], buf, off)
                out.append(v)
            return out, off
        if "option" in tref:
            tag = buf[off]
            off += 1
            if tag == 0:
                return None, off
            return self.decode(tref["option"], buf, off)
        if "array" in tref:
            inner, n = tref["array"]
            if inner == "u8":
                return buf[off:off + n].hex(), off + n
            out = []
            for _ in range(n):
                v, off = self.decode(inner, buf, off)
                out.append(v)
            return out, off
        raise ValueError(f"unsupported type ref: {tref}")

    def _decode_primitive(self, name: str, buf: bytes, off: int) -> tuple[Any, int]:
        if name == "pubkey":
            return base58.b58encode(buf[off:off + 32]).decode(), off + 32
        if name in ("bytes", "string"):
            n = struct.unpack_from("<I", buf, off)[0]
            off += 4
            raw = buf[off:off + n]
            return (raw.decode("utf-8", "replace") if name == "string" else raw.hex()), off + n
        if name == "bool":
            return buf[off] != 0, off + 1
        size = _PRIMITIVE_SIZES[name]
        fmt = {1: "B", 2: "H", 4: "I", 8: "Q", 16: None}[size]
        if size == 16:
            v = int.from_bytes(buf[off:off + 16], "little", signed=name.startswith("i"))
        else:
            v = struct.unpack_from(f"<{fmt.lower() if name.startswith('i') else fmt}", buf, off)[0]
        return v, off + size

    def _decode_defined(self, name: str, buf: bytes, off: int) -> tuple[Any, int]:
        t = self.types[name]
        if t["kind"] == "struct":
            out: dict[str, Any] = {}
            for f in t.get("fields") or []:
                out[f["name"]], off = self.decode(f["type"], buf, off)
            return out, off
        if t["kind"] == "enum":
            tag = buf[off]
            off += 1
            variants = t["variants"]
            if tag >= len(variants):
                raise ValueError(f"{name}: enum tag {tag} out of range")
            var = variants[tag]
            fields = var.get("fields")
            if not fields:
                return {"__variant": var["name"]}, off
            vals: dict[str, Any] = {"__variant": var["name"]}
            for i, f in enumerate(fields):
                if isinstance(f, dict) and "name" in f:
                    vals[f["name"]], off = self.decode(f["type"], buf, off)
                else:
                    vals[f"_{i}"], off = self.decode(f, buf, off)
            return vals, off
        raise ValueError(f"unsupported kind {t['kind']} for {name}")


_decoder: IdlDecoder | None = None


def get_decoder() -> IdlDecoder:
    global _decoder
    if _decoder is None:
        _decoder = IdlDecoder(json.loads(IDL_PATH.read_text()))
    return _decoder


def decode_action_proposal(raw: bytes) -> dict:
    """Header fields + decoded actions of one ActionProposal account."""
    if raw[:8] != ACTION_PROPOSAL_DISCRIMINATOR:
        raise ValueError("not an ActionProposal account")
    d = get_decoder()
    off = 8
    out: dict[str, Any] = {}
    out["vault"], off = d.decode("pubkey", raw, off)
    out["proposal_id"], off = d.decode("u64", raw, off)
    out["proposer"], off = d.decode("pubkey", raw, off)
    action_hex, off = d.decode("bytes", raw, off)
    out["created_at"], off = d.decode("i64", raw, off)
    out["voting_ends_at"], off = d.decode("i64", raw, off)
    out["timelock_seconds"], off = d.decode("u32", raw, off)
    out["executable_at"], off = d.decode("i64", raw, off)
    status, off = d.decode("u8", raw, off)
    out["status"] = PROPOSAL_STATUS[status] if status < len(PROPOSAL_STATUS) else str(status)
    out["reject_votes"], off = d.decode("u64", raw, off)
    out["opt_out_votes"], off = d.decode("u64", raw, off)
    out["bump"], off = d.decode("u8", raw, off)
    out["lp_supply_snapshot"], off = d.decode("u64", raw, off)

    # action_data serializes Vec<ProposalAction>: u32 count, then entries.
    action_buf = bytes.fromhex(action_hex)
    actions: list[Any] = []
    if len(action_buf) >= 4:
        n = struct.unpack_from("<I", action_buf, 0)[0]
        a_off = 4
        for _ in range(n):
            v, a_off = d.decode({"defined": {"name": "ProposalAction"}}, action_buf, a_off)
            actions.append(v)
    out["actions"] = actions
    return out


# ---------- summarizer ----------

def _walk(node: Any, pubkeys: list[str], slices: list[bytes]) -> None:
    """Collect every pubkey string and U8Slice payload in a decoded tree."""
    if isinstance(node, dict):
        if node.get("__variant") == "U8Slice" and "_0" in node:
            slices.append(bytes.fromhex(node["_0"]))
        for v in node.values():
            _walk(v, pubkeys, slices)
    elif isinstance(node, list):
        for v in node:
            _walk(v, pubkeys, slices)
    elif isinstance(node, str) and 32 <= len(node) <= 44:
        try:
            if len(base58.b58decode(node)) == 32:
                pubkeys.append(node)
        except ValueError:
            pass


def summarize_action(action: dict) -> dict:
    """Flatten one decoded ProposalAction into a UI display row."""
    kind = action.get("__variant")
    out: dict[str, Any] = {"kind": kind, "name": kind, "mode": None,
                           "programIds": [], "pubkeys": [], "verbs": []}

    inner = None
    if kind == "PolicyAction":
        inner = action.get("_0") or {}
        mode = inner.get("__variant")          # Add / Update / Remove
        out["mode"] = mode
        out["name"] = f"{mode} Policy"
    elif kind == "VaultSettingsAction":
        settings = action.get("_0") or []
        names = [s.get("__variant", "?") for s in settings]
        out["name"] = "Vault Settings"
        out["mode"] = ", ".join(names[:6])
        inner = settings
    elif kind == "PositionUpdate":
        inner = action.get("_0") or {}
        out["name"] = "Position Update"
        out["mode"] = inner.get("__variant")

    pubkeys: list[str] = []
    slices: list[bytes] = []
    _walk(inner, pubkeys, slices)

    # program_ids appear as InstructionConstraint.program_id — walk finds all
    # pubkeys; the caller maps known program ids to protocol names and known
    # mints to symbols, so just dedupe here.
    seen = set()
    out["pubkeys"] = [p for p in pubkeys if not (p in seen or seen.add(p))]

    verbs, vseen = [], set()
    for s in slices:
        verb = IX_DISC_VERBS.get(s[:8]) if len(s) >= 8 else None
        label = verb or (f"ix {s[:8].hex()}" if len(s) >= 8 else None)
        if label and label not in vseen:
            vseen.add(label)
            verbs.append(label)
    out["verbs"] = verbs
    return out
