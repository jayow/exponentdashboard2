"""Tests for extract_signatures — mock Helius, write to a temp DuckDB."""
from __future__ import annotations
import pytest
import respx
import httpx
import duckdb
from unittest.mock import patch

from extract_load import extract_signatures as es
from extract_load.helius_client import HeliusClient
from extract_load.load import RAW_DDL


URL = "https://mainnet.helius-rpc.com/?api-key=K1"


@pytest.fixture
def con():
    """In-memory DuckDB with raw schema already created."""
    c = duckdb.connect(":memory:")
    for ddl in RAW_DDL.values():
        c.execute(ddl)
    try:
        yield c
    finally:
        c.close()


def _resp(result):
    return httpx.Response(200, json={"jsonrpc": "2.0", "id": 1, "result": result})


async def _scan(con, sigs_pages):
    """Helper: mock the network with the given paginated responses, run scan."""
    with respx.mock() as mock:
        mock.post(URL).mock(side_effect=[_resp(p) for p in sigs_pages])
        async with HeliusClient(keys=["K1"]) as client:
            return await es.scan_address(client, con, "ADDR1")


@pytest.mark.asyncio
async def test_first_run_full_backfill(con):
    page1 = [{"signature": f"s{i:04d}", "slot": 100 + i, "blockTime": 1000 + i, "err": None} for i in range(1000)]
    page2 = [{"signature": f"t{i:04d}", "slot": 200 + i, "blockTime": 2000 + i, "err": None} for i in range(50)]
    # Last page < 1000 → iterator terminates
    r = await _scan(con, [page1, page2])

    assert r["mode"] == "full"
    assert r["new"] == 1050
    assert r["fully"] is True

    count = con.execute("SELECT COUNT(*) FROM raw_signatures").fetchone()[0]
    assert count == 1050

    state = es._state(con, "ADDR1")
    assert state["is_fully_backfilled"] is True
    # newest is the FIRST sig we saw (head); oldest is the last
    assert state["newest_sig"] == "s0000"
    assert state["oldest_sig"] == "t0049"


@pytest.mark.asyncio
async def test_incremental_after_fully_backfilled(con):
    # Seed: fully backfilled, newest=oldsig0
    con.execute(
        "INSERT INTO raw_signatures (signature, address, block_time, slot) VALUES (?, ?, ?, ?)",
        ["oldsig0", "ADDR1", 999, 50],
    )
    es._upsert_state(con, "ADDR1", is_fully_backfilled=True, newest_sig="oldsig0", oldest_sig="oldsig0")

    # New page with 3 fresh sigs (< 1000 so iterator stops after first page)
    new_page = [
        {"signature": "new1", "slot": 1000, "blockTime": 5000, "err": None},
        {"signature": "new2", "slot": 1001, "blockTime": 5001, "err": None},
        {"signature": "new3", "slot": 1002, "blockTime": 5002, "err": None},
    ]
    r = await _scan(con, [new_page])

    assert r["mode"] == "incremental"
    assert r["new"] == 3
    # Total: original seed + 3 new
    total = con.execute("SELECT COUNT(*) FROM raw_signatures").fetchone()[0]
    assert total == 4

    state = es._state(con, "ADDR1")
    assert state["newest_sig"] == "new1"  # most recent in this run


@pytest.mark.asyncio
async def test_idempotent_reinsert(con):
    # Insert same sig twice — second should be ignored, no error
    es._flush(con, [("dup", "ADDR1", 1, 1, None)])
    es._flush(con, [("dup", "ADDR1", 1, 1, None)])
    count = con.execute("SELECT COUNT(*) FROM raw_signatures WHERE signature='dup'").fetchone()[0]
    assert count == 1


@pytest.mark.asyncio
async def test_state_table_starts_empty_then_persists(con):
    assert es._state(con, "X") is None
    es._upsert_state(con, "X", is_fully_backfilled=True, newest_sig="a", oldest_sig="b")
    s = es._state(con, "X")
    assert s["is_fully_backfilled"] is True
    assert s["newest_sig"] == "a"

    # Update partial — preserves untouched fields
    es._upsert_state(con, "X", newest_sig="c")
    s = es._state(con, "X")
    assert s["newest_sig"] == "c"
    assert s["oldest_sig"] == "b"  # unchanged
    assert s["is_fully_backfilled"] is True  # unchanged


@pytest.mark.asyncio
async def test_err_field_serialized_as_json(con):
    """Solana err can be a complex object or None — store as JSON string."""
    page = [
        {"signature": "ok", "slot": 1, "blockTime": 1, "err": None},
        {"signature": "fail", "slot": 2, "blockTime": 2, "err": {"InstructionError": [0, "Custom"]}},
    ]
    await _scan(con, [page])
    rows = con.execute("SELECT signature, err FROM raw_signatures ORDER BY signature").fetchall()
    # ok → null; fail → JSON-encoded dict
    assert rows[0][0] == "fail"
    assert rows[0][1] is not None and "InstructionError" in rows[0][1]
    assert rows[1][0] == "ok"
    assert rows[1][1] is None
