"""Async Solana JSON-RPC client.

Provider-agnostic — accepts a list of pre-formed endpoint URLs (Helius,
QuickNode, Chainstack, Alchemy, public RPC, etc). All major providers
speak standard Solana JSON-RPC for the methods we use.

Features:
  - N-endpoint round-robin via per-endpoint semaphores
  - JSON-RPC batching for getTransaction (one HTTP, N results)
  - Sig pagination helper
  - Tenacity retries on 429/5xx/network errors with exponential backoff
  - Client-side rate limiters: token buckets for the global RPS cap AND
    a separate, much tighter bucket for getProgramAccounts (per Helius's
    documented "additional rate limiting beyond standard RPS" for it).

All methods return parsed JSON. Permanent 4xx (e.g. 400) raises immediately;
transient errors retry with backoff.

Why rate limits live in the client, not just the plan:
  Our Mac runs the extractors successfully because residential RTT (~100ms
  to Helius edge) naturally paces our asyncio code under the cap. Cloud
  hosts (GHA, Railway) have sub-15ms RTT — the same code bursts 100+ RPS
  and trips Helius's 429s even though we're nowhere near our daily CU
  budget. The buckets below replicate the natural pacing of slow networks
  for fast networks.
"""
from __future__ import annotations
import asyncio
import itertools
import time
from contextlib import asynccontextmanager
from typing import Any, AsyncIterator

import httpx
from tenacity import (
    retry,
    stop_after_attempt,
    retry_if_exception_type,
)


TRANSIENT_STATUS = {408, 429, 500, 502, 503, 504}

# Helius Developer plan caps: 50 RPS RPC total, plus an undocumented
# per-method cap on getProgramAccounts that's far lower. Set the global
# bucket below the headline cap (jitter, batched calls counted as N).
# Override with env vars HELIUS_RPS_CAP / HELIUS_GPA_RPS_CAP if you
# upgrade to a paid plan with more headroom.
import os
_RPC_RPS_CAP = float(os.environ.get("HELIUS_RPS_CAP", "40"))
_GPA_RPS_CAP = float(os.environ.get("HELIUS_GPA_RPS_CAP", "2"))


class TokenBucket:
    """asyncio-friendly token bucket for rate limiting.

    Each `acquire()` consumes 1 token. Tokens refill continuously at `rate`
    tokens/sec up to `capacity`. If no tokens available, blocks until one
    refills. Many concurrent acquirers share the same bucket cleanly.

    Implementation: lock-protected refill arithmetic, sleep outside the
    lock so other waiters can race for the next-available token.
    """
    __slots__ = ("rate", "capacity", "tokens", "last_refill", "_lock")

    def __init__(self, rate: float, capacity: float | None = None) -> None:
        self.rate = rate
        self.capacity = capacity if capacity is not None else rate
        self.tokens = self.capacity
        self.last_refill: float | None = None
        self._lock = asyncio.Lock()

    async def acquire(self) -> None:
        while True:
            async with self._lock:
                now = time.monotonic()
                if self.last_refill is None:
                    self.last_refill = now
                elapsed = now - self.last_refill
                self.tokens = min(self.capacity, self.tokens + elapsed * self.rate)
                self.last_refill = now
                if self.tokens >= 1.0:
                    self.tokens -= 1.0
                    return
                wait_time = (1.0 - self.tokens) / self.rate
            await asyncio.sleep(wait_time)


# Module-level buckets, shared across all SolanaRpcClient instances in
# this process. The asyncio.Lock inside each bucket is bound to whatever
# event loop first acquires it; in practice extractors run a single
# asyncio.run(main()) so this is fine.
_RPC_BUCKET = TokenBucket(rate=_RPC_RPS_CAP)
_GPA_BUCKET = TokenBucket(rate=_GPA_RPS_CAP)


def _is_gpa_body(body: Any) -> bool:
    """Detect getProgramAccounts calls in either a dict or list body."""
    if isinstance(body, dict):
        return body.get("method") == "getProgramAccounts"
    if isinstance(body, list):
        return any(b.get("method") == "getProgramAccounts" for b in body if isinstance(b, dict))
    return False


class TransientHTTPError(Exception):
    """Raised on retryable HTTP statuses so tenacity sees a specific type.

    Carries the Retry-After header value (seconds) if present, so the
    retry wait callback can honor it instead of pure exponential backoff.
    """
    def __init__(self, message: str, retry_after: float | None = None) -> None:
        super().__init__(message)
        self.retry_after = retry_after


def _wait_with_retry_after(retry_state: Any) -> float:
    """tenacity wait callback. If the last 429 came with a Retry-After
    header, honor that value (Helius told us exactly when to retry).
    Else fall back to capped exponential backoff: 1, 2, 4, 8, 16, 32, 60, 60."""
    exc = retry_state.outcome.exception() if retry_state.outcome else None
    if isinstance(exc, TransientHTTPError) and exc.retry_after is not None:
        return min(exc.retry_after, 60.0)
    return min(60.0, 2 ** (retry_state.attempt_number - 1))


class SolanaRpcClient:
    def __init__(
        self,
        endpoints: list[str],
        concurrency_per_endpoint: int = 2,
        timeout: float = 30.0,
    ):
        if not endpoints:
            raise ValueError("at least one RPC endpoint URL required")
        self.endpoints = endpoints
        self.semaphores = [asyncio.Semaphore(concurrency_per_endpoint) for _ in endpoints]
        self.client = httpx.AsyncClient(timeout=timeout)
        self._counter = itertools.count()

    async def __aenter__(self) -> "SolanaRpcClient":
        return self

    async def __aexit__(self, *exc) -> None:
        await self.close()

    async def close(self) -> None:
        await self.client.aclose()

    @asynccontextmanager
    async def _acquire(self, forced_url: str | None = None) -> AsyncIterator[str]:
        """Yield the URL of the next-available endpoint, holding its semaphore.

        If forced_url is given (must be one of self.endpoints), always use
        that endpoint instead of round-robining — still respects its
        semaphore for concurrency control.
        """
        if forced_url is not None:
            idx = self.endpoints.index(forced_url)
            sem = self.semaphores[idx]
            await sem.acquire()
            try:
                yield self.endpoints[idx]
            finally:
                sem.release()
            return
        start = next(self._counter) % len(self.endpoints)
        for offset in range(len(self.endpoints)):
            idx = (start + offset) % len(self.endpoints)
            sem = self.semaphores[idx]
            if not sem.locked():
                await sem.acquire()
                try:
                    yield self.endpoints[idx]
                finally:
                    sem.release()
                return
        # All semaphores saturated — block on the chosen one
        sem = self.semaphores[start]
        await sem.acquire()
        try:
            yield self.endpoints[start]
        finally:
            sem.release()

    @retry(
        # 9 attempts (~4 min total: 1+2+4+8+16+32+60+60). 11 was too many —
        # combined with the CI gPA throttle it pushed whole runs past the
        # timeout. 9 rides out normal Helius bursts; strategy-first ordering
        # + the publish guard cover the rare call that still exhausts it.
        # Wait strategy below honors Retry-After header when present.
        stop=stop_after_attempt(9),
        wait=_wait_with_retry_after,
        retry=retry_if_exception_type((TransientHTTPError, httpx.RequestError)),
        reraise=True,
    )
    async def _post(self, body: Any, *, endpoint: str | None = None) -> Any:
        # Client-side rate-limit BEFORE issuing the request. Two buckets:
        # global RPC and getProgramAccounts-specific. Cloud hosts burst
        # past Helius's caps without this; residential networks pace
        # themselves naturally by RTT and don't need it.
        await _RPC_BUCKET.acquire()
        if _is_gpa_body(body):
            await _GPA_BUCKET.acquire()
        async with self._acquire(endpoint) as url:
            resp = await self.client.post(url, json=body)
            if resp.status_code in TRANSIENT_STATUS:
                # Capture Retry-After header (RFC 7231 §7.1.3) so the retry
                # wait callback can use it instead of exponential guessing.
                ra_raw = resp.headers.get("retry-after")
                retry_after: float | None = None
                if ra_raw:
                    try:
                        retry_after = float(ra_raw)
                    except ValueError:
                        # HTTP-date form; we ignore and fall back to exponential.
                        pass
                raise TransientHTTPError(
                    f"{resp.status_code} from {url}", retry_after=retry_after
                )
            resp.raise_for_status()
            data = resp.json()
            # Some providers return HTTP 200 with a JSON-RPC-level error
            # object (plan-gated method, disabled method, etc) instead of a
            # transient HTTP status. Left unchecked, callers like
            # get_program_accounts do `result.get("result") or []` and
            # silently treat this as "zero accounts" — indistinguishable
            # from an empty-but-valid result. Raise so tenacity retries and
            # rotates to the next endpoint in the pool instead.
            # EXCEPT getAsset: Helius DAS reports a missing asset ("Asset
            # Not Found") as a JSON-RPC error too. That's a normal per-mint
            # miss, not an endpoint fault — get_asset's contract is to
            # return None for it, so pass the body through unchanged rather
            # than burning ~4 min of retries per unmetadata'd mint.
            is_get_asset = isinstance(body, dict) and body.get("method") == "getAsset"
            if isinstance(data, dict) and "error" in data and not is_get_asset:
                raise TransientHTTPError(f"JSON-RPC error from {url}: {data['error']}")
            return data

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
        """Yield every sig for an address, newest-to-oldest per page."""
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
        """Batched getTransaction. Returns one result per sig, aligned to input order."""
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

    async def get_asset(self, mint: str) -> dict | None:
        """Helius DAS getAsset — reads on-chain Metaplex Token Metadata.

        Returns the asset dict with content.metadata.{name,symbol},
        token_info.{decimals,symbol}, etc. None if the mint has no
        Metaplex metadata account.

        Only works against Helius endpoints (DAS is a Helius extension).
        Pinned to the first Helius endpoint in the pool (matched by
        hostname) so it never round-robins onto a non-Helius provider,
        which would otherwise silently return None (data loss) for every
        mint until the round-robin cycled back to Helius.
        """
        body = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "getAsset",
            "params": {"id": mint},
        }
        helius_endpoint = next(
            (e for e in self.endpoints if "helius-rpc.com" in e), None
        )
        result = await self._post(body, endpoint=helius_endpoint)
        return result.get("result")

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
# Run with: python -m extract_load.solana_rpc_client
# Requires SOLANA_RPC_URLS in .env (comma-separated full URLs).


async def _smoke() -> None:
    from .config import RPC_ENDPOINTS, EXPONENT_CORE_PROGRAM
    from rich import print as rprint

    if not RPC_ENDPOINTS:
        rprint("[red]No SOLANA_RPC_URLS in .env — cannot run smoke test[/red]")
        return

    rprint(
        f"[bold]SolanaRpcClient smoke test[/bold] — {len(RPC_ENDPOINTS)} endpoint(s) configured"
    )
    async with SolanaRpcClient(RPC_ENDPOINTS) as client:
        rprint(f"[cyan]getSignaturesForAddress[/cyan] {EXPONENT_CORE_PROGRAM} (limit=5)")
        sigs = await client.get_signatures_for_address(EXPONENT_CORE_PROGRAM, limit=5)
        rprint(f"  got {len(sigs)} sig(s)")
        for s in sigs[:3]:
            rprint(
                f"    {s.get('signature', '?')[:32]}...  slot={s.get('slot')}  err={s.get('err')}"
            )

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
