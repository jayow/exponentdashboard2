"""Discover Exponent markets: API for labels, on-chain for truth.

Two independent passes write to raw_markets with distinct source values:
  - source='api'      hits https://api.exponent.finance/markets
  - source='onchain'  getProgramAccounts on Exponent core, decode MarketThree

Both keyed on market_key (e.g. 'fragSOL-15DEC26'). dbt's stg_markets unions
them with API rows preferred for labels and on-chain rows filling expired-only
markets where the API has dropped them.

The on-chain pass is the source of truth — the API is a convenience layer that
gives us human-readable labels for active markets.
"""
from __future__ import annotations
import asyncio
import base64
import json
from collections import defaultdict
from datetime import datetime, timezone
from typing import Any

import base58
import duckdb
import httpx
from rich import print as rprint

from .config import RPC_ENDPOINTS, EXPONENT_CORE_PROGRAM
from .solana_rpc_client import SolanaRpcClient
from .load import warehouse
from .market_three_decoder import (
    MARKET_THREE_DISCRIMINATOR,
    decode as decode_market_three,
)


EXPONENT_API_URL = "https://api.exponent.finance/markets"
MONTH_ABBR = ("JAN", "FEB", "MAR", "APR", "MAY", "JUN",
              "JUL", "AUG", "SEP", "OCT", "NOV", "DEC")


def format_market_key(ticker: str, maturity_ts: int) -> str:
    """{TICKER}-{DDMonYY} in uppercase, e.g. 'fragSOL-15DEC26'.
    Ticker case is preserved from input — the dash and date are appended.
    """
    dt = datetime.fromtimestamp(maturity_ts, tz=timezone.utc)
    return f"{ticker}-{dt.day:02d}{MONTH_ABBR[dt.month - 1]}{dt.year % 100:02d}"


# ---------- API source ----------

async def fetch_api_markets(timeout: float = 30.0) -> list[dict]:
    """Hit api.exponent.finance/markets. Returns list of raw market dicts."""
    async with httpx.AsyncClient(timeout=timeout) as client:
        resp = await client.get(EXPONENT_API_URL)
        resp.raise_for_status()
        return resp.json()


def api_market_to_row(m: dict) -> tuple[str, dict] | None:
    """Normalize one API market into (market_key, payload). Returns None if
    required fields are missing (defensive).

    Address fields:
      - vault: market PDA (always)
      - ammPool: legacyMarketAddresses[0] — the AMM pool where SY↔PT swaps
        happen. Required for trading volume on AMM markets.
      - clmmOrderbook: orderbookAddresses[0] — the CLMM orderbook account.
        Markets are EITHER AMM or CLMM (sometimes both during migration).
      - pool: convenience alias = ammPool ?? clmmOrderbook (callers that
        don't care which kind use this).
    """
    sy_mint = m.get("syMint")
    ts = m.get("maturityDateUnixTs")
    underlying = m.get("underlyingAsset") or {}
    ticker = underlying.get("ticker") or ""
    if not (sy_mint and ts and ticker):
        return None
    market_key = format_market_key(ticker, ts)

    legacy = m.get("legacyMarketAddresses") or []
    orderbook = m.get("orderbookAddresses") or []
    amm_pool = legacy[0] if legacy else None
    clmm_orderbook = orderbook[0] if orderbook else None

    payload = {
        "syMint": sy_mint,
        "vault": m.get("vaultAddress"),
        "ptMint": m.get("ptMint"),
        "ytMint": m.get("ytMint"),
        "lpMint": m.get("lpMint"),
        "ammPool": amm_pool,
        "clmmOrderbook": clmm_orderbook,
        "pool": amm_pool or clmm_orderbook,
        "legacyMarketAddresses": legacy,
        "orderbookAddresses": orderbook,
        "underlying": underlying.get("mint"),
        "underlyingTicker": ticker,
        "underlyingDecimals": m.get("decimals", 6),
        "platform": m.get("platformName") or m.get("platform") or "",
        "maturityTs": ts,
        "maturityDate": datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d"),
        "marketStatus": m.get("marketStatus") or "active",
        "syExchangeRate": m.get("syExchangeRate"),
        "interfaceType": m.get("interfaceType", ""),
        # AMM pool reserves (raw atom units, divide by decimals for human).
        # Match Exponent's UI "Liquidity" via legacyLiquidity / 10^decimals × price.
        "legacyLiquidity": m.get("legacyLiquidity"),
        # Active TVL — PT_supply × underlying_USD, human units already.
        "totalMarketSize": m.get("totalMarketSize"),
    }
    return market_key, payload


# ---------- On-chain source ----------

async def fetch_onchain_market_accounts(client: SolanaRpcClient) -> list[dict]:
    """getProgramAccounts on Exponent core, filtered to MarketThree discriminator."""
    disc_b58 = base58.b58encode(MARKET_THREE_DISCRIMINATOR).decode()
    filters = [{"memcmp": {"offset": 0, "bytes": disc_b58}}]
    return await client.get_program_accounts(EXPONENT_CORE_PROGRAM, filters=filters)


def onchain_account_to_row(
    pubkey: str, account_data_b64: str, ticker_by_sy: dict[str, str]
) -> tuple[str, dict] | None:
    """Decode one MarketThree account into (market_key, payload).

    `ticker_by_sy` is built from the API pass so on-chain rows can borrow
    the ticker for the market_key. Expired-only SY mints without an API
    record get key='UNKNOWN-{date}'.
    """
    data = base64.b64decode(account_data_b64)
    decoded = decode_market_three(data)
    if decoded is None or decoded.maturity_ts is None:
        return None
    ticker = ticker_by_sy.get(decoded.sy_mint, "UNKNOWN")
    market_key = format_market_key(ticker, decoded.maturity_ts)
    payload = {
        "syMint": decoded.sy_mint,
        "ptMint": decoded.pt_mint,
        "lpMint": decoded.lp_mint,
        "vault": decoded.vault,
        "marketAccount": pubkey,
        "ammPool": pubkey,  # the MarketThree account IS the AMM pool
        "maturityTs": decoded.maturity_ts,
        "maturityDate": datetime.fromtimestamp(
            decoded.maturity_ts, tz=timezone.utc
        ).strftime("%Y-%m-%d"),
        "underlyingTicker": ticker,
        "rawSize": decoded.raw_size,
    }
    return market_key, payload


# ---------- Load ----------

def upsert_market(
    con: duckdb.DuckDBPyConnection, market_key: str, source: str, payload: dict
) -> None:
    con.execute(
        """
        INSERT INTO raw_markets (market_key, source, payload, fetched_at)
        VALUES (?, ?, ?, CURRENT_TIMESTAMP)
        ON CONFLICT (market_key, source) DO UPDATE SET
            payload    = excluded.payload,
            fetched_at = excluded.fetched_at
        """,
        [market_key, source, json.dumps(payload)],
    )


# ---------- Orchestrator ----------

async def run() -> dict:
    if not RPC_ENDPOINTS:
        raise RuntimeError("No SOLANA_RPC_URLS configured in .env")

    rprint("[cyan]Fetching Exponent API markets…[/cyan]")
    api_raw = await fetch_api_markets()
    rprint(f"  got {len(api_raw)} markets from API")

    api_rows: list[tuple[str, dict]] = []
    ticker_by_sy: dict[str, str] = {}
    for m in api_raw:
        row = api_market_to_row(m)
        if row is None:
            continue
        key, payload = row
        api_rows.append((key, payload))
        ticker_by_sy[payload["syMint"]] = payload["underlyingTicker"]

    rprint("[cyan]Fetching MarketThree accounts on-chain…[/cyan]")
    async with SolanaRpcClient(RPC_ENDPOINTS) as client:
        onchain_raw = await fetch_onchain_market_accounts(client)
        rprint(f"  got {len(onchain_raw)} MarketThree accounts on-chain")

        # Decode all candidates first — DON'T dedupe yet. Some markets
        # (notably fragSOL-10JUL25, ALP-20OCT25, JLP-28OCT25, USX-23JAN25)
        # have multiple MarketThree accounts on-chain because Exponent
        # re-deployed the market with a fresh PT mint mid-flight. The old
        # account lingers with a stale PT mint that has near-zero supply,
        # while the new account holds the actually-traded PT mint. Picking
        # the wrong one strands the SY-supply as "unsplit" at the protocol
        # level (the wrong PT mint shows zero PT/YT, so tvl_daily attributes
        # nothing to the market).
        candidates: dict[str, list[tuple[str, dict]]] = defaultdict(list)
        for acct in onchain_raw:
            pubkey = acct["pubkey"]
            data_field = acct["account"]["data"]
            data_b64 = data_field[0] if isinstance(data_field, list) else data_field
            row = onchain_account_to_row(pubkey, data_b64, ticker_by_sy)
            if row is None:
                continue
            key, payload = row
            candidates[key].append((pubkey, payload))

        onchain_rows: list[tuple[str, dict]] = []
        for key, entries in candidates.items():
            if len(entries) == 1:
                onchain_rows.append((key, entries[0][1]))
                continue
            # Same PT mint across all entries → arbitrary pick is fine.
            distinct_pt = {p["ptMint"] for _, p in entries}
            if len(distinct_pt) == 1:
                onchain_rows.append((key, entries[0][1]))
                continue
            # Different PT mints → query Metaplex to find the canonical one.
            # The live PT mint is named "Exponent PT-<ticker>-<DDMonYY>";
            # legacy/migration mints use other naming (e.g. lowercase "PT-fragSol-…").
            names: dict[str, str] = {}
            for pt in distinct_pt:
                asset = await client.get_asset(pt)
                names[pt] = ((asset or {}).get("content", {}).get("metadata", {}) or {}).get("name", "") or ""
            winner = next(
                (e for e in entries if names.get(e[1]["ptMint"], "").startswith("Exponent PT-")),
                None,
            )
            if winner is None:
                # No Metaplex-canonical name — fall back to largest account
                # (richer fields, more likely the active variant).
                winner = max(entries, key=lambda e: e[1].get("rawSize", 0))
            rprint(
                f"  [yellow]disambiguated[/yellow] {key}: "
                f"{len(entries)} candidates → kept {winner[0][:10]}… "
                f"(pt_mint name: {names.get(winner[1]['ptMint'], '?')!r})"
            )
            onchain_rows.append((key, winner[1]))

    rprint(f"  parsed {len(onchain_rows)} unique market_keys on-chain")

    api_count = 0
    onchain_count = 0
    with warehouse() as con:
        for key, payload in api_rows:
            upsert_market(con, key, "api", payload)
            api_count += 1
        for key, payload in onchain_rows:
            upsert_market(con, key, "onchain", payload)
            onchain_count += 1

    rprint(
        f"[green]Wrote raw_markets[/green]: "
        f"{api_count} api + {onchain_count} onchain"
    )
    return {"api": api_count, "onchain": onchain_count}


def main() -> None:
    started = datetime.now(timezone.utc)
    rprint(f"[bold]extract_markets[/bold]  started {started.isoformat()}")
    asyncio.run(run())
    rprint(f"[bold]done[/bold]  elapsed {datetime.now(timezone.utc) - started}")


if __name__ == "__main__":
    main()
