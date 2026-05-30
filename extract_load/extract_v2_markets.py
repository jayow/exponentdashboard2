"""Discover v2 (XPBook) market identities.

Pulls all XPBook-owned accounts (14 markets + 14 books), decodes the BOOK
headers (which embed 10 protocol-config pubkeys + maturity timestamp),
and writes one row per market to raw_v2_markets.

Ticker enrichment: for each book, the embedded pubkeys are checked against
v1 raw_markets payloads to inherit the underlying ticker (e.g. "ONyc",
"USX", "fragSOL") when the v2 market is the v2-side of an existing v1
market. Tickers we can't auto-resolve get '?'; hand-curate via a seed
later if needed.

Run: python -m extract_load.extract_v2_markets
"""
from __future__ import annotations
import asyncio
import base64
import json
from datetime import datetime, timezone

import base58
import httpx
from rich import print as rprint

from .config import RPC_ENDPOINTS
from .load import warehouse
from .v2_book_decoder import (
    BOOK_DISC_HEX, MARKET_DISC_HEX, decode_book,
)


XPBOOK_PROGRAM = "XPBookgQTN2p8Yw1C2La35XkPMmZTCEYH77AdReVvK1"
SPL_TOKEN_ACCOUNT_LEN = 165


def _underlying_from_metadata(symbol: str, name: str | None) -> str:
    """Recover the underlying ticker from an Exponent SY mint's metadata.

    Name pattern is canonical ("Exponent Wrapped <TICKER>"); symbol is a
    fallback. Falls back to the raw symbol for non-Exponent SY mints
    (e.g. kUSDG which is a Kamino token used directly).
    """
    if name and "Wrapped " in name:
        return name.split("Wrapped ", 1)[1].strip()
    if symbol.startswith("ew"):
        return symbol[2:]
    if symbol.startswith("w") and len(symbol) > 1:
        return symbol[1:]
    return symbol


async def _resolve_via_vault(con, pubkeys: list[str]) -> tuple[str, str | None]:
    """Probe each header pubkey on-chain; for the SPL TokenAccount among them
    (vault slot 5 in practice), read mint at bytes [0..32) and look up symbol.
    Returns (ticker, sy_mint) or ("?", None) on failure.
    """
    url = RPC_ENDPOINTS[0]
    # First pass: existing raw_token_metadata gives both name + symbol cheaply
    meta_index = {
        r[0]: (r[1], r[2]) for r in con.execute(
            "SELECT mint, name, symbol FROM raw_token_metadata WHERE symbol IS NOT NULL"
        ).fetchall()
    }
    async with httpx.AsyncClient(timeout=30.0) as client:
        for pk in pubkeys:
            resp = await client.post(url, json={
                "jsonrpc": "2.0", "id": 1, "method": "getAccountInfo",
                "params": [pk, {"encoding": "base64"}]
            })
            val = resp.json().get("result", {}).get("value")
            if not val:
                continue
            buf = base64.b64decode(val["data"][0])
            if len(buf) != SPL_TOKEN_ACCOUNT_LEN:
                continue
            sy_mint = base58.b58encode(buf[:32]).decode()
            name, sym = meta_index.get(sy_mint, (None, None))
            if not sym:
                asset_resp = await client.post(url, json={
                    "jsonrpc": "2.0", "id": 1, "method": "getAsset",
                    "params": {"id": sy_mint},
                })
                asset = asset_resp.json().get("result") or {}
                meta = (asset.get("content") or {}).get("metadata") or {}
                sym = meta.get("symbol")
                name = meta.get("name")
            if sym:
                return _underlying_from_metadata(sym, name), sy_mint
    return "?", None


async def fetch_xpbook_accounts() -> list[dict]:
    """Single getProgramAccounts call returns all 28 XPBook-owned accounts."""
    url = RPC_ENDPOINTS[0]
    async with httpx.AsyncClient(timeout=60.0) as client:
        resp = await client.post(url, json={
            "jsonrpc": "2.0", "id": 1, "method": "getProgramAccounts",
            "params": [XPBOOK_PROGRAM, {"encoding": "base64"}],
        })
        resp.raise_for_status()
        return resp.json()["result"]


def _load_v1_ticker_index(con) -> dict[str, str]:
    """Build pubkey → ticker index from v1 raw_markets payloads.
    Any pubkey appearing in a v1 market's payload (vault, ammPool,
    syMint, etc.) maps to that market's ticker. Used to inherit
    ticker labels for v2 markets that share v1 underlyings.
    """
    rows = con.execute("SELECT market_key, payload FROM raw_markets").fetchall()
    index: dict[str, str] = {}
    for market_key, payload_json in rows:
        try:
            p = json.loads(payload_json)
        except Exception:
            continue
        ticker = p.get("underlyingTicker") or market_key.split("-")[0]
        if not ticker:
            continue
        for k, v in p.items():
            if isinstance(v, str) and len(v) in (43, 44):  # base58 pubkey length
                index.setdefault(v, ticker)
            elif isinstance(v, list):
                for item in v:
                    if isinstance(item, str) and len(item) in (43, 44):
                        index.setdefault(item, ticker)
    return index


async def run() -> dict:
    if not RPC_ENDPOINTS:
        raise RuntimeError("No SOLANA_RPC_URLS configured in .env")

    rprint("[cyan]Fetching XPBook-owned accounts…[/cyan]")
    accounts = await fetch_xpbook_accounts()
    rprint(f"  got {len(accounts)} accounts owned by XPBook")

    markets: list[dict] = []
    books: list[dict] = []
    for a in accounts:
        data_field = a["account"]["data"]
        data_b64 = data_field[0] if isinstance(data_field, list) else data_field
        raw = base64.b64decode(data_b64)
        if raw[:8].hex() == MARKET_DISC_HEX:
            markets.append({"pubkey": a["pubkey"], "raw": raw})
        elif raw[:8].hex() == BOOK_DISC_HEX:
            books.append({"pubkey": a["pubkey"], "raw": raw})

    rprint(f"  classified: {len(markets)} markets + {len(books)} books")

    with warehouse() as con:
        ticker_by_pubkey = _load_v1_ticker_index(con)
        rprint(f"  v1 ticker index has {len(ticker_by_pubkey)} pubkey entries")

        # Pair each market account with its book by scanning the small market
        # account's bytes for the book pubkey. (Markets contain refs to books;
        # we verified earlier that books do NOT contain refs to markets.)
        # If no pubkey-level pairing is found, fall back to slot-order matching
        # (markets[i] paired with books[i] since getProgramAccounts is stable).
        market_to_book: dict[str, str] = {}
        for m in markets:
            for b in books:
                if base58.b58decode(b["pubkey"]) in m["raw"]:
                    market_to_book[m["pubkey"]] = b["pubkey"]
                    break

        # Fallback for any unpaired
        paired_books = set(market_to_book.values())
        unpaired_markets = [m for m in markets if m["pubkey"] not in market_to_book]
        unpaired_books = [b for b in books if b["pubkey"] not in paired_books]
        if len(unpaired_markets) == len(unpaired_books):
            for m, b in zip(unpaired_markets, unpaired_books):
                market_to_book[m["pubkey"]] = b["pubkey"]
                rprint(
                    f"  [yellow]pairing-by-order[/yellow] "
                    f"{m['pubkey'][:12]}… → {b['pubkey'][:12]}…"
                )

        # Now decode each book's header to extract maturity + identify underlying.
        n_written = 0
        for m in markets:
            book_pk = market_to_book.get(m["pubkey"])
            book = next((b for b in books if b["pubkey"] == book_pk), None)
            if book is None:
                rprint(f"  [yellow]skip {m['pubkey'][:12]}… — no book pair[/yellow]")
                continue
            decoded = decode_book(book["raw"])

            # Always probe vault[5] (SY-side SPL TokenAccount in book.pubkeys)
            # → mint at bytes [0..32) → symbol via raw_token_metadata or DAS.
            # Strip "w"/"ew" prefix → underlying ticker. Works for all markets,
            # including v2-natives without v1 counterparts.
            ticker, sy_mint = await _resolve_via_vault(con, decoded.pubkeys)
            # If on-chain probe didn't yield a symbol, fall back to v1 index scan
            if ticker == "?":
                for pk in decoded.pubkeys:
                    if pk in ticker_by_pubkey:
                        ticker = ticker_by_pubkey[pk]
                        break
            if ticker == "?":
                for off in range(0, len(m["raw"]) - 31):
                    candidate = base58.b58encode(m["raw"][off:off+32]).decode()
                    if candidate in ticker_by_pubkey:
                        ticker = ticker_by_pubkey[candidate]
                        break

            maturity_dt = datetime.fromtimestamp(decoded.expiration_ts, tz=timezone.utc)
            market_key = f"{ticker}-{maturity_dt.strftime('%d%b%y').upper()}"

            payload = {
                "market_account": m["pubkey"],
                "book_account":   book["pubkey"],
                "expiration_ts":  decoded.expiration_ts,
                "expiration_iso": maturity_dt.isoformat(),
                "price_decimals": decoded.price_decimals,
                "ln_maker_fee":   decoded.ln_maker_fee,
                "ln_taker_fee":   decoded.ln_taker_fee,
                "threshold_amount": decoded.threshold_amount,
                "config_pubkeys": decoded.pubkeys,
            }

            con.execute("""
                INSERT INTO raw_v2_markets (
                    market_account, book_account,
                    sy_mint, pt_mint, yt_mint,
                    underlying_ticker, maturity_ts, market_key,
                    payload, fetched_at
                )
                VALUES (?, ?, ?, NULL, NULL, ?, ?, ?, ?, CURRENT_TIMESTAMP)
                ON CONFLICT (market_account) DO UPDATE SET
                    book_account      = excluded.book_account,
                    sy_mint           = excluded.sy_mint,
                    underlying_ticker = excluded.underlying_ticker,
                    maturity_ts       = excluded.maturity_ts,
                    market_key        = excluded.market_key,
                    payload           = excluded.payload,
                    fetched_at        = excluded.fetched_at
            """, [
                m["pubkey"], book["pubkey"], sy_mint,
                ticker, decoded.expiration_ts, market_key,
                json.dumps(payload),
            ])
            n_written += 1
            rprint(
                f"  [green]✓[/green] {market_key:<24}  "
                f"book={book['pubkey'][:12]}…  matures={maturity_dt:%Y-%m-%d}"
            )

    rprint(f"[green]Wrote raw_v2_markets: {n_written} rows[/green]")
    return {"markets": n_written}


def main() -> None:
    started = datetime.now(timezone.utc)
    rprint(f"[bold]extract_v2_markets[/bold]  started {started.isoformat()}")
    asyncio.run(run())
    rprint(f"[bold]done[/bold]  elapsed {datetime.now(timezone.utc) - started}")


if __name__ == "__main__":
    main()
