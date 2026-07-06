"""Capture per-tx tranche deposits / withdrawals → raw_tranche_lp_actions.

Walks every signature touching each tranche vault PDA (incremental
watermark via the latest block_time we've seen), then decodes the txs
to extract per-wallet LP movements.

Instruction vocabulary recognized (from the Anchor IDL):
    Deposit           — raw program ix, user → vault
    Withdraw          — raw program ix, vault → user
    WrapperDeposit    — wraps base token → SY → LP (most common entry point)
    WrapperWithdraw   — burns LP → SY → base token (most common exit)

For each tx that hit the tranche program, we walk pre/post token balance
deltas FOR THE LP MINTS in the snapshot row (mint_lp_senior + mint_lp_junior).
A non-zero delta on a wallet-owned ATA = the action's effect for that
wallet. The same tx may emit two rows if it touches both legs (rare; usually
one or the other).

Sign convention: lp_amount_raw is POSITIVE for deposits (LP minted to user)
and NEGATIVE for withdrawals (LP burned from user). sy_amount_raw uses the
same sign convention (= SY locked vs SY returned).

This is best-effort: settlement / governance / cycle-rollover events are
captured in a sibling extractor (extract_tranche_events.py — TODO once the
first settle fires). This file focuses on the user-action surface.
"""
from __future__ import annotations
import argparse
import asyncio
import json
from datetime import datetime, timezone

from rich import print as rprint

from .config import RPC_ENDPOINTS, EXPONENT_TRANCHING_PROGRAM, EXTRACT_BATCH_SIZE
from .solana_rpc_client import SolanaRpcClient
from .load import warehouse


# IDL instruction names → discriminator-prefix-free human labels we store.
TRANCHE_IX_LABELS: dict[str, str] = {
    "Deposit":         "Deposit",
    "Withdraw":        "Withdraw",
    "WrapperDeposit":  "WrapperDeposit",
    "WrapperWithdraw": "WrapperWithdraw",
}


def _detect_tranche_ix(logs: list[str]) -> str | None:
    """Return the relevant tranche instruction name from log messages, or None."""
    for line in logs:
        if not line.startswith("Program log: Instruction: "):
            continue
        name = line[len("Program log: Instruction: "):].strip()
        if name in TRANCHE_IX_LABELS:
            return TRANCHE_IX_LABELS[name]
    return None


def _walk_lp_deltas(
    pre_balances: list[dict],
    post_balances: list[dict],
    sr_lp_mint: str,
    jr_lp_mint: str,
) -> list[tuple[str, str, str, int]]:
    """Return [(wallet, leg, lp_mint, delta_raw), …] for every wallet-LP-ATA
    that changed. Delta is post − pre in raw atom units."""
    pre = {b["accountIndex"]: b for b in pre_balances}
    post = {b["accountIndex"]: b for b in post_balances}
    out: list[tuple[str, str, str, int]] = []
    for idx in sorted(set(pre) | set(post)):
        a, b = pre.get(idx, {}), post.get(idx, {})
        mint = b.get("mint") or a.get("mint")
        if mint not in (sr_lp_mint, jr_lp_mint):
            continue
        owner = b.get("owner") or a.get("owner")
        if not owner:
            continue
        pre_amt = int((a.get("uiTokenAmount") or {}).get("amount", "0") or "0")
        post_amt = int((b.get("uiTokenAmount") or {}).get("amount", "0") or "0")
        delta = post_amt - pre_amt
        if delta == 0:
            continue
        leg = "SR" if mint == sr_lp_mint else "JR"
        out.append((owner, leg, mint, delta))
    return out


def _walk_sy_delta(
    pre_balances: list[dict],
    post_balances: list[dict],
    wallet: str,
    sy_mint: str,
) -> int:
    """Sum SY delta on all ATAs owned by `wallet` for `sy_mint`. Returns 0 if
    the wallet never touched SY in this tx (common — wrapper ixs may not
    expose direct SY balance changes)."""
    pre = {b["accountIndex"]: b for b in pre_balances}
    post = {b["accountIndex"]: b for b in post_balances}
    total = 0
    for idx in sorted(set(pre) | set(post)):
        a, b = pre.get(idx, {}), post.get(idx, {})
        mint = b.get("mint") or a.get("mint")
        owner = b.get("owner") or a.get("owner")
        if mint != sy_mint or owner != wallet:
            continue
        pre_amt = int((a.get("uiTokenAmount") or {}).get("amount", "0") or "0")
        post_amt = int((b.get("uiTokenAmount") or {}).get("amount", "0") or "0")
        total += (post_amt - pre_amt)
    return total


async def _process_vault(
    client: SolanaRpcClient,
    vault_pk: str,
    sr_lp_mint: str,
    jr_lp_mint: str,
    sy_mint: str,
    until_block_time: int,
    backfill_pages_max: int,
) -> list[tuple]:
    """Sweep signatures touching `vault_pk` newer than `until_block_time` and
    emit rows for raw_tranche_lp_actions."""
    sigs: list[dict] = []
    page = 0
    before: str | None = None
    while page < backfill_pages_max:
        chunk = await client.get_signatures_for_address(vault_pk, before=before, limit=1000)
        if not chunk:
            break
        sigs.extend(chunk)
        before = chunk[-1]["signature"]
        page += 1
        if (chunk[-1].get("blockTime") or 0) <= until_block_time:
            break

    # Trim to fresh sigs only
    fresh = [s for s in sigs if (s.get("blockTime") or 0) > until_block_time]
    if not fresh:
        return []

    rprint(f"    {vault_pk[:12]}…  fetching {len(fresh)} tx for decoding")
    # Batch get_transactions to respect Helius's per-request payload limit
    # (jsonParsed responses are large; batches > ~20 trigger HTTP 413).
    sig_list = [s["signature"] for s in fresh]
    txs: list[dict | None] = []
    for i in range(0, len(sig_list), EXTRACT_BATCH_SIZE):
        chunk_sigs = sig_list[i : i + EXTRACT_BATCH_SIZE]
        txs.extend(await client.get_transactions(chunk_sigs))

    rows: list[tuple] = []
    for sig_meta, tx in zip(fresh, txs):
        if not tx:
            continue
        meta = tx.get("meta") or {}
        if meta.get("err"):
            continue
        logs = meta.get("logMessages") or []
        ix_name = _detect_tranche_ix(logs)
        if ix_name is None:
            continue

        deltas = _walk_lp_deltas(
            meta.get("preTokenBalances") or [],
            meta.get("postTokenBalances") or [],
            sr_lp_mint,
            jr_lp_mint,
        )
        if not deltas:
            continue

        for wallet, leg, lp_mint, lp_delta in deltas:
            sy_delta = _walk_sy_delta(
                meta.get("preTokenBalances") or [],
                meta.get("postTokenBalances") or [],
                wallet,
                sy_mint,
            )
            # SY delta sign convention: deposits go INTO the vault so user SY
            # ATA loses balance (negative). We flip so the lp/sy signs match
            # (both positive on deposit, both negative on withdraw).
            rows.append((
                sig_meta["signature"],
                vault_pk,
                sig_meta.get("blockTime") or 0,
                tx.get("slot"),
                ix_name,
                wallet,
                leg,
                lp_mint,
                lp_delta,
                -sy_delta,
            ))

    return rows


async def run(full_refresh: bool = False) -> dict:
    if not RPC_ENDPOINTS:
        raise RuntimeError("No SOLANA_RPC_URLS configured in .env")

    started = datetime.now(timezone.utc)
    rprint(f"[cyan]extract_tranche_actions  started {started.isoformat()}[/cyan]")

    # Read tranche vaults + their LP mints + SY mint from the latest state snapshot.
    with warehouse() as con:
        vaults = con.execute(
            """SELECT tranche_vault, mint_lp_senior, mint_lp_junior, sy_mint
               FROM raw_tranche_states
               WHERE snapshot_date = (SELECT MAX(snapshot_date) FROM raw_tranche_states)"""
        ).fetchall()

    if not vaults:
        rprint("[yellow]  no tranche vaults in raw_tranche_states yet; run extract_tranche_states first[/yellow]")
        return {"vaults": 0, "rows": 0}

    rprint(f"  sweeping {len(vaults)} tranche vault PDA(s)")

    # Watermark per vault
    until_by_vault: dict[str, int] = {}
    with warehouse() as con:
        for vp, _, _, _ in vaults:
            if full_refresh:
                until_by_vault[vp] = 0
                continue
            r = con.execute(
                "SELECT COALESCE(MAX(block_time), 0) FROM raw_tranche_lp_actions WHERE tranche_vault = ?",
                [vp],
            ).fetchone()
            until_by_vault[vp] = int(r[0] or 0)

    all_rows: list[tuple] = []
    async with SolanaRpcClient(RPC_ENDPOINTS) as client:
        for vault_pk, sr_lp, jr_lp, sy_mint in vaults:
            until_ts = until_by_vault[vault_pk]
            # 5 pages × 1000 sigs = 5000 sig backfill ceiling per run (~50 days of
            # an active book at 100 sig/day); enough for initial sweep on this
            # vault (8 days old) and trivially small for incremental runs.
            rows = await _process_vault(
                client, vault_pk, sr_lp, jr_lp, sy_mint,
                until_block_time=until_ts,
                backfill_pages_max=5,
            )
            all_rows.extend(rows)

    rprint(f"  decoded {len(all_rows)} LP action rows")

    if all_rows:
        with warehouse() as con:
            con.executemany(
                """INSERT OR REPLACE INTO raw_tranche_lp_actions (
                    signature, tranche_vault, block_time, slot, ix_name,
                    wallet, leg, lp_mint, lp_amount_raw, sy_amount_raw
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                all_rows,
            )

    rprint(f"[green]Wrote {len(all_rows)} LP action rows[/green]  elapsed {datetime.now(timezone.utc) - started}")
    return {"vaults": len(vaults), "rows": len(all_rows)}


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--full-refresh", action="store_true",
                   help="Ignore watermark and walk back to the vault genesis.")
    args = p.parse_args()
    asyncio.run(run(full_refresh=args.full_refresh))


if __name__ == "__main__":
    main()
