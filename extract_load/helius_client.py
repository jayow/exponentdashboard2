"""Async Helius / Solana JSON-RPC client.

Features:
  - Dual-key (or N-key) round-robin via per-key semaphores
  - JSON-RPC batching for getTransaction (one HTTP, N results)
  - Sig pagination helper
  - Tenacity retries on 429/5xx/network errors with exponential backoff

Helius URL: https://mainnet.helius-rpc.com/?api-key={key}

All methods return parsed JSON. Error handling is conservative — transient
errors retry, malformed bodies raise. The caller decides what to do with
None results (missing tx, etc.).
"""
from __future__ import annotations
import asyncio
import itertools
from contextlib import asynccontextmanager
from typing import Any, AsyncIterator

import httpx
from tenacity import (
    retry,
    stop_after_attempt,
    wait_exponential,
    retry_if_exception_type,
)


HELIUS_URL_FMT = "https://mainnet.helius-rpc.com/?api-key={}"
TRANSIENT_STATUS = {429, 500, 502, 503, 504}


class TransientHTTPError(Exception):
    """Raised on retryable HTTP statuses so tenacity sees a specific type."""


class HeliusClient:
    def __init__(
        self,
        keys: list[str],
        concurrency_per_key: int = 12,
        timeout: float = 30.0,
        rpc_url_fmt: str = HELIUS_URL_FMT,
    ):
        if not keys:
            raise ValueError("at least one Helius key required")
        self.keys = keys
        self.semaphores = [asyncio.Semaphore(concurrency_per_key) for _ in keys]
        self.client = httpx.AsyncClient(timeout=timeout)
        self._counter = itertools.count()
        self._rpc_url_fmt = rpc_url_fmt

    async def __aenter__(self) -> "HeliusClient":
        return self

    async def __aexit__(self, *exc) -> None:
        await self.close()

    async def close(self) -> None:
        await self.client.aclose()

    def _url(self, key_idx: int) -> str:
        return self._rpc_url_fmt.format(self.keys[key_idx])

    @asynccontextmanager
    async def _acquire(self) -> AsyncIterator[str]:
        """Yield the URL of the next-available key, holding its semaphore."""
        start = next(self._counter) % len(self.keys)
        for offset in range(len(self.keys)):
            idx = (start + offset) % len(self.keys)
            sem = self.semaphores[idx]
            if not sem.locked():
                await sem.acquire()
                try:
                    yield self._url(idx)
                finally:
                    sem.release()
                return
        # all semaphores saturated — block on the chosen one
        sem = self.semaphores[start]
        await sem.acquire()
        try:
            yield self._url(start)
        finally:
            sem.release()

    @retry(
        stop=stop_after_attempt(5),
        wait=wait_exponential(multiplier=1, min=1, max=30),
        retry=retry_if_exception_type((TransientHTTPError, httpx.RequestError)),
        reraise=True,
    )
    async def _post(self, body: Any) -> Any:
        async with self._acquire() as url:
            resp = await self.client.post(url, json=body)
            if resp.status_code in TRANSIENT_STATUS:
                raise TransientHTTPError(f"{resp.status_code} from Helius")
            resp.raise_for_status()
            return resp.json()

    # ---------- High-level methods ----------

    async def get_signatures_for_address(
        self,
        address: str,
        before: str | None = None,
        until: str | None = None,
        limit: int = 1000,
    ) -> list[dict]:
        opts: dict[str, Any] = {"limit": limit}
        if before:
            opts["before"] = before
        if until:
            opts["until"] = until
        body = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "getSignaturesForAddress",
            "params": [address, opts],
        }
        result = await self._post(body)
        return result.get("result") or []

    async def iter_all_signatures(
        self, address: str, until: str | None = None, page_size: int = 1000
    ) -> AsyncIterator[dict]:
        """Yield every sig for an address, oldest-to-newest order preserved per page."""
        before: str | None = None
        while True:
            page = await self.get_signatures_for_address(
                address, before=before, until=until, limit=page_size
            )
            if not page:
                return
            for sig in page:
                yield sig
            if len(page) < page_size:
                return
            before = page[-1]["signature"]

    async def get_transactions(self, signatures: list[str]) -> list[dict | None]:
        """Batched getTransaction. Returns one result per signature, aligned to input order."""
        if not signatures:
            return []
        body = [
            {
                "jsonrpc": "2.0",
                "id": i,
                "method": "getTransaction",
                "params": [
                    sig,
                    {"encoding": "jsonParsed", "maxSupportedTransactionVersion": 0},
                ],
            }
            for i, sig in enumerate(signatures)
        ]
        result = await self._post(body)
        if isinstance(result, list):
            by_id: dict[int, Any] = {r["id"]: r.get("result") for r in result if "id" in r}
            return [by_id.get(i) for i in range(len(signatures))]
        # Single-element batch can come back as a dict
        return [result.get("result")]

    async def get_account_info(
        self, address: str, encoding: str = "base64"
    ) -> dict | None:
        body = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "getAccountInfo",
            "params": [address, {"encoding": encoding}],
        }
        result = await self._post(body)
        return (result.get("result") or {}).get("value")

    async def get_multiple_accounts(
        self, addresses: list[str], encoding: str = "base64"
    ) -> list[dict | None]:
        if not addresses:
            return []
        body = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "getMultipleAccounts",
            "params": [addresses, {"encoding": encoding}],
        }
        result = await self._post(body)
        return (result.get("result") or {}).get("value") or []

    async def get_program_accounts(
        self, program_id: str, filters: list[dict] | None = None, encoding: str = "base64"
    ) -> list[dict]:
        opts: dict[str, Any] = {"encoding": encoding}
        if filters:
            opts["filters"] = filters
        body = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "getProgramAccounts",
            "params": [program_id, opts],
        }
        result = await self._post(body)
        return result.get("result") or []


# ---------- CLI smoke test ----------
# Run with: python -m extract_load.helius_client
# Requires HELIUS_KEY_1 (and optionally HELIUS_KEY_2) in .env.
# Hits real network with 1 small request — no warehouse writes.


async def _smoke() -> None:
    from .config import HELIUS_KEYS, EXPONENT_PROGRAM
    from rich import print as rprint

    if not HELIUS_KEYS:
        rprint("[red]No HELIUS_KEY_* in .env — cannot run smoke test[/red]")
        return

    rprint(f"[bold]HeliusClient smoke test[/bold] — {len(HELIUS_KEYS)} key(s) configured")
    async with HeliusClient(HELIUS_KEYS) as client:
        rprint(f"[cyan]getSignaturesForAddress[/cyan] {EXPONENT_PROGRAM} (limit=5)")
        sigs = await client.get_signatures_for_address(EXPONENT_PROGRAM, limit=5)
        rprint(f"  got {len(sigs)} sig(s)")
        for s in sigs[:3]:
            rprint(f"    {s.get('signature', '?')[:32]}...  slot={s.get('slot')}  err={s.get('err')}")

        if sigs:
            rprint(f"[cyan]getTransactions (batched, n={min(2, len(sigs))})[/cyan]")
            txs = await client.get_transactions([s["signature"] for s in sigs[:2]])
            for t in txs:
                if t:
                    bt = t.get("blockTime")
                    n_inner = len((t.get("meta") or {}).get("innerInstructions") or [])
                    rprint(f"    blockTime={bt}  innerIxGroups={n_inner}")
                else:
                    rprint("    [yellow]null result[/yellow]")
    rprint("[green]smoke ok[/green]")


def main() -> None:
    asyncio.run(_smoke())


if __name__ == "__main__":
    main()
