"""Probe ExponentStrategyVault byte layout + event channel against live data.

Run: python -m extract_load.probe_strategy_vaults

What it does:
  1. Loads the ExponentStrategyVault IDL, prints the account field order
     (Borsh serializes in-order — this is our ground truth for offsets).
  2. Fetches all ExponentStrategyVault accounts via getProgramAccounts
     (memcmp on the 8-byte discriminator at offset 0).
  3. For each vault: pubkey, data length, first 200 bytes hex, and the
     four pubkey-decoded values at offsets 8, 40, 72, 104 as instructed.
     (NB: per the IDL, offset 8 is a [u8;32] array, and offset 104 is a
     Vec<TokenEntry> length prefix — not a pubkey — so the offset=104
     decode should look like garbage; that IS the signal.)
  4. For the FIRST vault: pulls the last 20 signatures, walks through
     them (successful only, up to a bounded number), fetches the full
     tx with maxSupportedTransactionVersion=0, and inspects the
     logMessages / innerInstructions for the event channel.

Read on stdout: is Anchor emitting events via "Program data:" log lines
(base64-encoded Anchor Event with 8-byte discriminator prefix), via a
self-CPI (event_cpi), or neither?
"""
from __future__ import annotations
import asyncio
import base64
import json
from pathlib import Path

import base58

from .config import RPC_ENDPOINTS
from .solana_rpc_client import SolanaRpcClient


STRATEGY_VAULT_PROGRAM = "sVau1tXvayVWfotzm9Ahcv2qfnnfRWttt78BCnNC6dD"
STRATEGY_VAULT_DISC = bytes([98, 228, 39, 201, 116, 210, 39, 11])
IDL_PATH = Path(__file__).resolve().parent.parent / "idls" / "exponent_strategy_vault.json"

# Bounded budget for tx probing on the first vault
MAX_TX_TO_PROBE = 5


def _b58(buf: bytes, off: int, length: int = 32) -> str:
    return base58.b58encode(buf[off:off + length]).decode()


def _describe_field_type(t) -> str:
    """Cheap pretty-printer for Anchor IDL type entries."""
    if isinstance(t, str):
        return t
    if isinstance(t, dict):
        if "array" in t:
            inner, n = t["array"]
            return f"[{_describe_field_type(inner)}; {n}]"
        if "vec" in t:
            return f"Vec<{_describe_field_type(t['vec'])}>"
        if "option" in t:
            return f"Option<{_describe_field_type(t['option'])}>"
        if "defined" in t:
            d = t["defined"]
            if isinstance(d, dict):
                return d.get("name", str(d))
            return str(d)
    return json.dumps(t)


def print_idl_field_order() -> None:
    with IDL_PATH.open() as f:
        idl = json.load(f)
    struct_def = None
    for t in idl.get("types", []):
        if t.get("name") == "ExponentStrategyVault":
            struct_def = t
            break
    if struct_def is None:
        print("!! IDL missing ExponentStrategyVault type entry")
        return
    print("=" * 72)
    print("IDL FIELD ORDER — ExponentStrategyVault (Borsh, in-order)")
    print("=" * 72)
    fields = struct_def["type"]["fields"]
    for i, fld in enumerate(fields):
        tstr = _describe_field_type(fld.get("type"))
        print(f"  [{i:2}] {fld['name']:<32} : {tstr}")
    print()
    print("First 4 slots per task, decoded as pubkey from offsets 8/40/72/104:")
    print("  offset   8..40  →", fields[0]["name"], "(", _describe_field_type(fields[0]["type"]), ")")
    print("  offset  40..72  →", fields[1]["name"], "(", _describe_field_type(fields[1]["type"]), ")")
    print("  offset  72..104 →", fields[2]["name"], "(", _describe_field_type(fields[2]["type"]), ")")
    print("  offset 104..136 →", fields[3]["name"], "(", _describe_field_type(fields[3]["type"]), ")")
    print()
    print("EVENT LIST (name → discriminator = sha256('event:<Name>')[:8]):")
    for e in idl.get("events", []):
        disc = e.get("discriminator")
        if disc:
            hex_disc = bytes(disc).hex()
        else:
            hex_disc = "(none)"
        print(f"  - {e['name']:<40} disc={hex_disc}")
    print()


async def probe() -> None:
    if not RPC_ENDPOINTS:
        raise RuntimeError("No SOLANA_RPC_URLS configured in .env")

    print_idl_field_order()

    async with SolanaRpcClient(RPC_ENDPOINTS) as client:
        print("=" * 72)
        print("STEP 1 — getProgramAccounts for ExponentStrategyVault")
        print("=" * 72)
        print(f"program: {STRATEGY_VAULT_PROGRAM}")
        print(f"filter : memcmp offset=0 bytes={base58.b58encode(STRATEGY_VAULT_DISC).decode()}")
        print(f"        (discriminator hex = {STRATEGY_VAULT_DISC.hex()})")

        accts = await client.get_program_accounts(
            STRATEGY_VAULT_PROGRAM,
            filters=[
                {"memcmp": {"offset": 0, "bytes": base58.b58encode(STRATEGY_VAULT_DISC).decode()}}
            ],
        )
        print(f"\nfound {len(accts)} account(s)\n")
        if not accts:
            print("!! no ExponentStrategyVault accounts returned — cannot probe further")
            return

        for i, a in enumerate(accts):
            pubkey = a["pubkey"]
            data_field = a["account"]["data"]
            data_b64 = data_field[0] if isinstance(data_field, list) else data_field
            raw = base64.b64decode(data_b64)
            print("-" * 72)
            print(f"[vault {i}] {pubkey}")
            print(f"           data length = {len(raw)} bytes")
            print(f"           first 200 bytes hex:")
            hx = raw[:200].hex()
            for row in range(0, len(hx), 64):
                print(f"             {hx[row:row+64]}")
            print(f"           disc check   = {raw[:8].hex()} "
                  f"({'MATCH' if raw[:8] == STRATEGY_VAULT_DISC else 'MISMATCH'})")

            # Decode "first 4 pubkey fields" at offsets 8/40/72/104 as pubkeys.
            # Per the IDL these positions actually hold:
            #   8   : [u8;32] nav_aum_circuit_breaker_state (raw bytes as pubkey form)
            #   40  : pubkey  squads_settings
            #   72  : pubkey  squads_vault
            #   104 : u32 vec length prefix of token_entries + then TokenEntry array
            #         (so the 32 bytes there are NOT a pubkey)
            print(f"           off 8  as-pubkey: {_b58(raw, 8)}")
            print(f"           off 40 as-pubkey: {_b58(raw, 40)}   (IDL: squads_settings)")
            print(f"           off 72 as-pubkey: {_b58(raw, 72)}   (IDL: squads_vault)")
            # Show 104 raw as u32 vec length, then bytes 108..108+32 as pk attempt.
            u32_len = int.from_bytes(raw[104:108], "little")
            print(f"           off 104 u32 vec_len (token_entries) = {u32_len}")
            print(f"           off 104 as-pubkey (WRONG expected): {_b58(raw, 104)}")
            print(f"           off 108 as-pubkey (first TokenEntry field): {_b58(raw, 108)}")
            print()

        # Focus on first vault for tx probing
        vault_pk = accts[0]["pubkey"]
        print("=" * 72)
        print(f"STEP 2 — getSignaturesForAddress for first vault {vault_pk}")
        print("=" * 72)
        sigs = await client.get_signatures_for_address(vault_pk, limit=20)
        print(f"received {len(sigs)} signature(s)")
        for s in sigs[:10]:
            print(f"  slot={s.get('slot')}  err={s.get('err')}  sig={s.get('signature')}")
        print()

        successful = [s for s in sigs if s.get("err") is None]
        if not successful:
            print("!! no successful signatures on this vault in the last 20 — stopping tx probe")
            return
        print(f"{len(successful)} successful in last 20; probing up to {MAX_TX_TO_PROBE}")

        # Fetch the batched TXs
        to_fetch = [s["signature"] for s in successful[:MAX_TX_TO_PROBE]]
        # get_transactions uses jsonParsed encoding with maxSupportedTransactionVersion=0
        # per solana_rpc_client.py — matches the task spec.
        txs = await client.get_transactions(to_fetch)

        for idx, (sig, tx) in enumerate(zip(to_fetch, txs)):
            print("-" * 72)
            print(f"[tx {idx}] sig={sig}")
            if tx is None:
                print("  <no result / null>")
                continue
            meta = tx.get("meta") or {}
            slot = tx.get("slot")
            bt = tx.get("blockTime")
            print(f"  slot={slot}  blockTime={bt}  err={meta.get('err')}")

            log_msgs = meta.get("logMessages") or []
            print(f"  logMessages ({len(log_msgs)}):")
            for lm in log_msgs:
                print(f"    | {lm}")

            program_data = [lm for lm in log_msgs if lm.startswith("Program data:")]
            print(f"  Program-data log lines: {len(program_data)}")
            for pd in program_data:
                # Base64 payload after "Program data: "
                b64 = pd[len("Program data: "):].strip()
                try:
                    raw = base64.b64decode(b64)
                    disc = raw[:8].hex()
                    print(f"    payload={len(raw)}B  disc(hex)={disc}  b64={b64[:60]}...")
                except Exception as e:
                    print(f"    (decode failed: {e})  b64={b64[:60]}...")

            inner = meta.get("innerInstructions") or []
            print(f"  innerInstructions groups: {len(inner)}")
            for group in inner:
                gidx = group.get("index")
                ixs = group.get("instructions") or []
                print(f"    group index={gidx}  #ixs={len(ixs)}")
                for j, ix in enumerate(ixs):
                    prog = ix.get("programId") or ix.get("program") or "?"
                    accts_used = ix.get("accounts") or []
                    data_field = ix.get("data")
                    parsed = ix.get("parsed")
                    kind = "parsed" if parsed else "raw"
                    print(f"      [{j}] prog={prog}  kind={kind}  "
                          f"n_accts={len(accts_used)}  data={str(data_field)[:60] if data_field else '-'}")
                    # If inner ix's programId is the strategy vault program itself,
                    # this could be Anchor's event_cpi channel.
                    if prog == STRATEGY_VAULT_PROGRAM:
                        print(f"          !! self-CPI to strategy vault program (candidate event_cpi)")

        print()
        print("=" * 72)
        print("SUMMARY HINTS")
        print("=" * 72)
        print("- 'Program data:' log lines are Anchor's default event channel")
        print("  (base64 = 8-byte event disc + Borsh-encoded event struct).")
        print("- Self-CPIs back to the same program with data starting with the")
        print("  event discriminator indicate the newer #[event_cpi] channel.")
        print("- If neither shows up, events aren't emitted on this codepath.")


def main() -> None:
    asyncio.run(probe())


if __name__ == "__main__":
    main()
