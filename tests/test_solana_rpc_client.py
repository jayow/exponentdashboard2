"""Offline unit tests for SolanaRpcClient — mock the network, exercise the wiring.

Live network smoke is in `python -m extract_load.solana_rpc_client`.
"""
from __future__ import annotations
import asyncio
import json

import httpx
import pytest
import respx

from extract_load.solana_rpc_client import SolanaRpcClient, TransientHTTPError


URL = "https://rpc1.example/"
URL2 = "https://rpc2.example/"


def _ok(body):
    return httpx.Response(200, json=body)


@pytest.fixture
def client():
    return SolanaRpcClient(endpoints=[URL], concurrency_per_endpoint=4)


@pytest.mark.asyncio
async def test_get_signatures_for_address_returns_list(client):
    with respx.mock(assert_all_called=True) as mock:
        mock.post(URL).mock(return_value=_ok({
            "jsonrpc": "2.0",
            "id": 1,
            "result": [
                {"signature": "sig1", "slot": 100, "blockTime": 1, "err": None},
                {"signature": "sig2", "slot": 101, "blockTime": 2, "err": None},
            ],
        }))
        try:
            sigs = await client.get_signatures_for_address("ADDR", limit=2)
        finally:
            await client.close()
        assert len(sigs) == 2
        assert sigs[0]["signature"] == "sig1"


@pytest.mark.asyncio
async def test_get_signatures_empty_returns_empty_list(client):
    with respx.mock() as mock:
        mock.post(URL).mock(return_value=_ok({"jsonrpc": "2.0", "id": 1, "result": []}))
        try:
            sigs = await client.get_signatures_for_address("ADDR")
        finally:
            await client.close()
        assert sigs == []


@pytest.mark.asyncio
async def test_get_signatures_null_result(client):
    """Helius sometimes returns result: null for unknown addresses."""
    with respx.mock() as mock:
        mock.post(URL).mock(return_value=_ok({"jsonrpc": "2.0", "id": 1, "result": None}))
        try:
            sigs = await client.get_signatures_for_address("ADDR")
        finally:
            await client.close()
        assert sigs == []


@pytest.mark.asyncio
async def test_iter_all_signatures_paginates(client):
    """First page returns 1000, second page returns < 1000, stops."""
    page1 = [{"signature": f"s{i}"} for i in range(1000)]
    page2 = [{"signature": f"t{i}"} for i in range(5)]

    with respx.mock() as mock:
        responses = [
            _ok({"jsonrpc": "2.0", "id": 1, "result": page1}),
            _ok({"jsonrpc": "2.0", "id": 1, "result": page2}),
        ]
        mock.post(URL).mock(side_effect=responses)
        try:
            seen = [s async for s in client.iter_all_signatures("ADDR")]
        finally:
            await client.close()
    assert len(seen) == 1005
    assert seen[0]["signature"] == "s0"
    assert seen[-1]["signature"] == "t4"


@pytest.mark.asyncio
async def test_get_transactions_batched_returns_aligned(client):
    """Sends a JSON-RPC batch, results re-ordered by id."""
    sigs = ["sigA", "sigB", "sigC"]
    # Helius returns out-of-order — client must realign by id
    batch_response = [
        {"jsonrpc": "2.0", "id": 2, "result": {"blockTime": 30, "meta": {}}},
        {"jsonrpc": "2.0", "id": 0, "result": {"blockTime": 10, "meta": {}}},
        {"jsonrpc": "2.0", "id": 1, "result": None},
    ]
    with respx.mock() as mock:
        route = mock.post(URL).mock(return_value=_ok(batch_response))
        try:
            txs = await client.get_transactions(sigs)
        finally:
            await client.close()

        # Verify body was a JSON-RPC batch (list)
        sent = json.loads(route.calls.last.request.content)
        assert isinstance(sent, list)
        assert len(sent) == 3
        assert sent[0]["method"] == "getTransaction"

    assert txs[0]["blockTime"] == 10
    assert txs[1] is None
    assert txs[2]["blockTime"] == 30


@pytest.mark.asyncio
async def test_get_transactions_empty_input(client):
    try:
        result = await client.get_transactions([])
    finally:
        await client.close()
    assert result == []


@pytest.mark.asyncio
async def test_retry_on_429_then_success(client):
    """Tenacity should retry on 429 and eventually return."""
    with respx.mock() as mock:
        responses = [
            httpx.Response(429, text="rate limit"),
            httpx.Response(429, text="rate limit"),
            _ok({"jsonrpc": "2.0", "id": 1, "result": []}),
        ]
        mock.post(URL).mock(side_effect=responses)
        try:
            # Should not raise — retry succeeds on third attempt
            sigs = await client.get_signatures_for_address("ADDR")
        finally:
            await client.close()
        assert sigs == []


@pytest.mark.asyncio
async def test_400_does_not_retry(client):
    """Permanent 4xx (not 429) should raise immediately, not retry."""
    with respx.mock() as mock:
        route = mock.post(URL).mock(return_value=httpx.Response(400, text="bad request"))
        try:
            with pytest.raises(httpx.HTTPStatusError):
                await client.get_signatures_for_address("ADDR")
        finally:
            await client.close()
        # Only one attempt — no retry
        assert route.call_count == 1


@pytest.mark.asyncio
async def test_endpoint_round_robin():
    """Two requests on a client with 2 endpoints should hit both URLs."""
    client = SolanaRpcClient(endpoints=[URL, URL2], concurrency_per_endpoint=4)
    body = {"jsonrpc": "2.0", "id": 1, "result": []}
    with respx.mock() as mock:
        r1 = mock.post(URL).mock(return_value=_ok(body))
        r2 = mock.post(URL2).mock(return_value=_ok(body))
        try:
            await client.get_signatures_for_address("A")
            await client.get_signatures_for_address("B")
        finally:
            await client.close()
        # With round-robin and free semaphores, both endpoints should have been hit.
        assert r1.call_count + r2.call_count == 2
        assert r1.called and r2.called


def test_requires_at_least_one_endpoint():
    with pytest.raises(ValueError):
        SolanaRpcClient(endpoints=[])
