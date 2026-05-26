"""Resolve symbol/name/decimals for every mint Exponent touches.

Two passes:

1. Exponent API `/tokens` → raw_exponent_tokens
   Returns ~63 base-asset tokens (the canonical underlyings the protocol
   trades against: USX, fragSOL, JitoSOL, USDC, ...). Symbol-keyed.

2. Helius DAS `getAsset` → raw_token_metadata
   For every distinct mint we've seen in raw_markets (sy/pt/yt/lp), read
   the Metaplex Token Metadata account. Exponent's SY mints follow a
   `name: 'Exponent Wrapped X'` convention; downstream models parse the
   ticker out of that for expired markets.

Idempotent: ON CONFLICT DO UPDATE refreshes name/symbol/decimals on each run.
"""
from __future__ import annotations
import asyncio
import json
from datetime import datetime, timezone

import duckdb
import httpx
from rich import print as rprint
from rich.progress import (
    BarColumn,
    MofNCompleteColumn,
    Progress,
    SpinnerColumn,
    TextColumn,
    TimeElapsedColumn,
)

from .config import RPC_ENDPOINTS
from .solana_rpc_client import SolanaRpcClient
from .load import warehouse


EXPONENT_TOKENS_URL = "https://api.exponent.finance/tokens"


# ---------- Pass 1: /tokens ----------

async def fetch_exponent_tokens(timeout: float = 30.0) -> list[dict]:
    """Returns [] on 401/403 so pipeline survives upstream access changes.
    Existing raw_exponent_tokens rows are preserved (UPSERT, not delete).
    Pass 2 (DAS) runs regardless.
    """
    async with httpx.AsyncClient(timeout=timeout) as c:
        try:
            r = await c.get(EXPONENT_TOKENS_URL)
            r.raise_for_status()
            return r.json()
        except httpx.HTTPStatusError as e:
            if e.response.status_code in (401, 403):
                rprint(
                    f"[yellow]Exponent /tokens auth-gated "
                    f"({e.response.status_code}) — keeping cached rows[/yellow]"
                )
                return []
            raise


def _upsert_exponent_token(con: duckdb.DuckDBPyConnection, t: dict) -> None:
    mint = t.get("mint")
    if not mint:
        return
    con.execute(
        """
        INSERT INTO raw_exponent_tokens (mint, name, symbol, decimals, payload, fetched_at)
        VALUES (?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
        ON CONFLICT (mint) DO UPDATE SET
            name = excluded.name,
            symbol = excluded.symbol,
            decimals = excluded.decimals,
            payload = excluded.payload,
            fetched_at = excluded.fetched_at
        """,
        [mint, t.get("name"), t.get("symbol"), t.get("decimals"), json.dumps(t)],
    )


# ---------- Pass 2: DAS getAsset ----------

def _market_related_mints(con: duckdb.DuckDBPyConnection) -> list[str]:
    """Collect every distinct mint that appears in Exponent-related data.

    Sources:
      1. raw_markets.payload (sy/pt/yt/lp/underlying — explicit market mints)
      2. raw_helius_tx pre/postTokenBalances (catches PT/YT/LP mints of
         expired markets that our on-chain MarketThree decoder doesn't
         extract — its `pt_mint` etc. fields are NULL).
      3. raw_exponent_tokens (Exponent's /tokens universe — to fill in
         metadata for underlying tokens that lack Metaplex names).

    Returns mints NOT already in raw_token_metadata, so re-running is fast.
    """
    rows = con.execute(
        """
        WITH from_markets AS (
            SELECT mint FROM (
                SELECT payload->>'$.syMint'   AS mint FROM raw_markets
                UNION ALL
                SELECT payload->>'$.ptMint'   AS mint FROM raw_markets
                UNION ALL
                SELECT payload->>'$.ytMint'   AS mint FROM raw_markets
                UNION ALL
                SELECT payload->>'$.lpMint'   AS mint FROM raw_markets
                UNION ALL
                SELECT payload->>'$.underlying' AS mint FROM raw_markets
            ) WHERE mint IS NOT NULL
        ),
        from_tx_balances AS (
            -- Every mint that appeared in a token-balance entry across our 568K txs.
            -- We materialize via stg_token_changes for fast scan.
            SELECT DISTINCT mint FROM main_staging.stg_token_changes
        ),
        from_tokens AS (
            SELECT mint FROM raw_exponent_tokens
        ),
        all_mints AS (
            SELECT mint FROM from_markets
            UNION
            SELECT mint FROM from_tx_balances
            UNION
            SELECT mint FROM from_tokens
        )
        SELECT DISTINCT a.mint
        FROM all_mints a
        WHERE a.mint IS NOT NULL
          AND NOT EXISTS (SELECT 1 FROM raw_token_metadata m WHERE m.mint = a.mint)
        """
    ).fetchall()
    return [r[0] for r in rows]


def _upsert_token_metadata(
    con: duckdb.DuckDBPyConnection, mint: str, result: dict | None
) -> bool:
    """Upsert one mint's DAS metadata. Returns True if metadata existed."""
    if not result:
        # Insert NULL row so we don't re-fetch this mint
        con.execute(
            """
            INSERT INTO raw_token_metadata (mint, name, symbol, decimals, source, payload, fetched_at)
            VALUES (?, NULL, NULL, NULL, 'das', NULL, CURRENT_TIMESTAMP)
            ON CONFLICT (mint) DO UPDATE SET fetched_at = excluded.fetched_at
            """,
            [mint],
        )
        return False
    md = (result.get("content") or {}).get("metadata") or {}
    ti = result.get("token_info") or {}
    name = md.get("name")
    symbol = md.get("symbol") or ti.get("symbol")
    decimals = ti.get("decimals")
    con.execute(
        """
        INSERT INTO raw_token_metadata (mint, name, symbol, decimals, source, payload, fetched_at)
        VALUES (?, ?, ?, ?, 'das', ?, CURRENT_TIMESTAMP)
        ON CONFLICT (mint) DO UPDATE SET
            name = excluded.name,
            symbol = excluded.symbol,
            decimals = excluded.decimals,
            payload = excluded.payload,
            fetched_at = excluded.fetched_at
        """,
        [mint, name, symbol, decimals, json.dumps(result)],
    )
    return bool(name or symbol)


async def run() -> dict:
    if not RPC_ENDPOINTS:
        raise RuntimeError("No SOLANA_RPC_URLS configured in .env")

    # Pass 1
    rprint("[cyan]Fetching /tokens from Exponent API…[/cyan]")
    tokens = await fetch_exponent_tokens()
    rprint(f"  got {len(tokens)} tokens")

    with warehouse() as con:
        for t in tokens:
            _upsert_exponent_token(con, t)
        rprint(f"  wrote {len(tokens)} rows to raw_exponent_tokens")

        # Pass 2
        mints = _market_related_mints(con)
        rprint(f"[cyan]Fetching DAS metadata for {len(mints)} market-related mints…[/cyan]")

        n_with_meta = 0
        async with SolanaRpcClient(RPC_ENDPOINTS, concurrency_per_endpoint=8) as client:
            with Progress(
                SpinnerColumn(),
                TextColumn("[progress.description]{task.description}"),
                BarColumn(),
                MofNCompleteColumn(),
                TimeElapsedColumn(),
                transient=True,
            ) as bar:
                task = bar.add_task("fetching", total=len(mints))
                # Sequential is fine here — only 30-60 calls
                for mint in mints:
                    try:
                        result = await client.get_asset(mint)
                    except Exception as e:
                        rprint(f"  [yellow]warn[/yellow] {mint[:24]}... → {e}")
                        result = None
                    if _upsert_token_metadata(con, mint, result):
                        n_with_meta += 1
                    bar.update(task, advance=1)

    rprint(
        f"[green]Done[/green]  tokens={len(tokens)}  mints_queried={len(mints)}  "
        f"with_metadata={n_with_meta}"
    )
    return {"tokens": len(tokens), "mints": len(mints), "with_metadata": n_with_meta}


def main() -> None:
    started = datetime.now(timezone.utc)
    rprint(f"[bold]extract_token_metadata[/bold]  started {started.isoformat()}")
    asyncio.run(run())
    rprint(f"[bold]done[/bold]  elapsed {datetime.now(timezone.utc) - started}")


if __name__ == "__main__":
    main()
