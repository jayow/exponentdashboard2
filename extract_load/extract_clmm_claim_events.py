"""Capture CLMM farm-claim events for empirical formula calibration.

Anchor events on Exponent's CLMM are defined in the IDL but the program
does NOT emit them via `Program data:` log lines (verified by scanning
hundreds of ClaimFarmEmissions txs — zero Program data instances). So we
fall back to a more robust source of truth: the token balance delta.

Every `ClaimFarmEmissions` or `MarketCollectEmission` instruction
transfers tokens from the protocol's reward escrow to the user's ATA.
That delta is observable in tx meta:
  amount_claimed = postTokenBalances[user_ata].amount
                 − preTokenBalances[user_ata].amount

We harvest these (signature, block_time, owner, mint, amount, lp_position)
tuples. Joined later with raw_positions snapshots taken near the claim
block, each row becomes a (state, amount) calibration sample for fitting
the CLMM farm-reward formula.

After ~50-100 samples across diverse positions, the formula can be
derived statistically and validated before going live in the mart.

Instruction names recognized (from exponent-clmm-idl.json):
  • claim_farm_emission       → 'ClaimFarmEmissions' in log Instruction lines
  • market_collect_emission   → 'MarketCollectEmission'
"""
from __future__ import annotations
import argparse
import json
import re
from datetime import datetime, timezone

from rich import print as rprint

from .config import EXPONENT_CLMM_PROGRAM
from .load import warehouse


CLAIM_INSTRUCTIONS = ("ClaimFarmEmissions", "MarketCollectEmission")
RE_INVOKE = re.compile(r"^Program ([A-Za-z0-9]{32,44}) invoke \[\d+\]$")
RE_IX     = re.compile(r"^Program log: Instruction: (\w+)$")


def _find_claim_instructions(logs: list[str]) -> list[tuple[int, str]]:
    """Return list of (log_index, instruction_name) for every CLMM
    claim/collect-emission ix in the log stream."""
    out = []
    program_stack: list[str] = []
    for i, log in enumerate(logs):
        m_in = RE_INVOKE.match(log)
        if m_in:
            program_stack.append(m_in.group(1))
            continue
        if log.startswith("Program ") and (" success" in log or " failed" in log):
            if program_stack:
                program_stack.pop()
            continue
        m_ix = RE_IX.match(log)
        if m_ix and program_stack and program_stack[-1] == EXPONENT_CLMM_PROGRAM:
            ix = m_ix.group(1)
            if ix in CLAIM_INSTRUCTIONS:
                out.append((i, ix))
    return out


def _extract_owner_and_amount(meta: dict, account_keys: list[str]) -> list[dict]:
    """For each token balance delta where someone GAINED tokens, return
    {owner, mint, amount}. The owner of the largest gain in a claim tx is
    the claimer; the mint is the farm reward token."""
    pre  = {b["accountIndex"]: b for b in (meta.get("preTokenBalances") or [])}
    post = {b["accountIndex"]: b for b in (meta.get("postTokenBalances") or [])}
    rows = []
    for idx, post_b in post.items():
        pre_b = pre.get(idx)
        post_amt = int((post_b.get("uiTokenAmount") or {}).get("amount", "0"))
        pre_amt  = int((pre_b.get("uiTokenAmount") or {}).get("amount", "0")) if pre_b else 0
        delta = post_amt - pre_amt
        if delta <= 0:
            continue
        rows.append({
            "owner":  post_b.get("owner"),
            "mint":   post_b.get("mint"),
            "amount": delta,
            "account_index": idx,
            "ata":    account_keys[idx] if idx < len(account_keys) else None,
        })
    return rows


def run(full_refresh: bool = False) -> dict:
    rprint(f"[cyan]Scanning raw_helius_tx for CLMM claim/collect events…[/cyan]")
    rows: list[tuple] = []
    with warehouse() as con:
        if full_refresh:
            con.execute("DELETE FROM raw_clmm_claim_events")
            cutoff_ts = 0
        else:
            cutoff_ts = con.execute(
                "SELECT COALESCE(MAX(block_time), 0) FROM raw_clmm_claim_events"
            ).fetchone()[0] or 0
        rprint(f"  cutoff block_time: {cutoff_ts}")
        tx_rows = con.execute(
            """SELECT signature, block_time, payload FROM raw_helius_tx
               WHERE block_time > ? AND payload IS NOT NULL
                 AND payload LIKE '%ClaimFarmEmissions%' ESCAPE '\\'
                  OR (block_time > ? AND payload IS NOT NULL
                      AND payload LIKE '%MarketCollectEmission%' ESCAPE '\\')""",
            [cutoff_ts, cutoff_ts],
        ).fetchall()
        rprint(f"  candidate tx with claim keyword in payload: {len(tx_rows):,}")
        for sig, bt, payload in tx_rows:
            try:
                p = json.loads(payload)
            except Exception:
                continue
            meta = p.get("meta") or {}
            logs = meta.get("logMessages") or []
            claims = _find_claim_instructions(logs)
            if not claims:
                continue
            # All token-balance gains in this tx — usually 1 row (the claimer).
            account_keys = (p.get("transaction") or {}).get("message", {}).get("accountKeys") or []
            if not account_keys:
                account_keys = [k.get("pubkey") if isinstance(k, dict) else k for k in account_keys]
            gains = _extract_owner_and_amount(meta, account_keys)
            for log_idx, ix_name in claims:
                # Pair each claim instruction with the largest gain (heuristic:
                # one claim per tx in practice — confirmed via sampling).
                if not gains:
                    continue
                top = max(gains, key=lambda g: g["amount"])
                rows.append((
                    sig, log_idx, bt, ix_name,
                    top["owner"], None,  # market_address not parseable from logs alone
                    None,  # lp_position — would need to be derived from instruction account list
                    top["mint"], None, top["amount"], None, None, None, None, None, None,
                    None, None, None, None,
                ))
        if rows:
            con.executemany(
                """INSERT OR IGNORE INTO raw_clmm_claim_events
                   (signature, log_index, block_time, event_type,
                    owner_address, market_address, lp_position, mint,
                    farm_index, amount_claimed, remaining_staged, lp_balance,
                    lower_tick, upper_tick, tokens_owed_pt, tokens_owed_sy,
                    farm_lsi, farm_staged, total_lp_share, n_shares)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                rows,
            )
        total = con.execute("SELECT COUNT(*) FROM raw_clmm_claim_events").fetchone()[0]

    rprint(f"[green]Wrote {len(rows)} new claim rows ({total} total in calibration corpus)[/green]")
    return {"new_events": len(rows), "total_events": total}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--full-refresh", action="store_true")
    args = parser.parse_args()
    started = datetime.now(timezone.utc)
    rprint(f"[bold]extract_clmm_claim_events[/bold]  started {started.isoformat()}")
    run(full_refresh=args.full_refresh)
    rprint(f"[bold]done[/bold]  elapsed {datetime.now(timezone.utc) - started}")


if __name__ == "__main__":
    main()
