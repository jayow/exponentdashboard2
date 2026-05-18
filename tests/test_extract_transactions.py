"""Tests for extract_transactions — mock Helius batch responses, real DuckDB."""
from __future__ import annotations
import json

import duckdb
import httpx
import pytest
import respx

from extract_load import extract_transactions as et
from extract_load.load import RAW_DDL, _apply_column_migrations


URL = "https://rpc.example/"


@pytest.fixture
def con():
    c = duckdb.connect(":memory:")
    for ddl in RAW_DDL.values():
        c.execute(ddl)
    _apply_column_migrations(c)
    try:
        yield c
    finally:
        c.close()


def _seed_sig(con, sig, *, block_time=1000, err=None):
    con.execute(
        "INSERT INTO raw_signatures (signature, address, discovered_via, block_time, slot, err) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        [sig, "ADDR", "core_program", block_time, 1, json.dumps(err) if err else None],
    )


def _seed_tx(con, sig, *, block_time=1000):
    con.execute(
        "INSERT INTO raw_helius_tx (signature, block_time, slot, payload) VALUES (?, ?, ?, ?)",
        [sig, block_time, 1, json.dumps({"blockTime": block_time})],
    )


# ---------- missing_sigs ----------

def test_missing_sigs_returns_empty_when_no_signatures(con):
    assert et.missing_sigs(con) == []


def test_missing_sigs_returns_unfetched_only(con):
    _seed_sig(con, "fetched")
    _seed_tx(con, "fetched")
    _seed_sig(con, "missing1", block_time=2000)
    _seed_sig(con, "missing2", block_time=3000)
    rows = et.missing_sigs(con)
    assert [r[0] for r in rows] == ["missing2", "missing1"]  # desc by block_time


def test_missing_sigs_skips_failed_txs(con):
    _seed_sig(con, "ok", block_time=1000)
    _seed_sig(con, "failed", block_time=2000, err={"InstructionError": [0, "Custom"]})
    rows = et.missing_sigs(con)
    assert [r[0] for r in rows] == ["ok"]


def test_missing_sigs_respects_limit(con):
    for i in range(10):
        _seed_sig(con, f"s{i}", block_time=1000 + i)
    rows = et.missing_sigs(con, limit=3)
    assert len(rows) == 3
    # Newest 3 by block_time desc
    assert [r[0] for r in rows] == ["s9", "s8", "s7"]


def test_missing_sigs_asc_order(con):
    for i in range(5):
        _seed_sig(con, f"s{i}", block_time=1000 + i)
    rows = et.missing_sigs(con, order="asc")
    assert [r[0] for r in rows] == ["s0", "s1", "s2", "s3", "s4"]


# ---------- _insert_chunk ----------

def test_insert_chunk_inserts_new(con):
    rows = [("sig1", 100, 1, json.dumps({"x": 1}))]
    n = et._insert_chunk(con, rows)
    assert n == 1
    assert con.execute("SELECT COUNT(*) FROM raw_helius_tx").fetchone()[0] == 1


def test_insert_chunk_on_conflict_no_op(con):
    et._insert_chunk(con, [("sig1", 100, 1, json.dumps({"x": 1}))])
    n = et._insert_chunk(con, [("sig1", 999, 1, json.dumps({"x": 2}))])
    assert n == 0  # nothing newly inserted
    # Original payload preserved
    payload = json.loads(con.execute("SELECT payload FROM raw_helius_tx").fetchone()[0])
    assert payload["x"] == 1


def test_insert_chunk_empty_is_safe(con):
    assert et._insert_chunk(con, []) == 0


# ---------- run() end-to-end with mocked batch RPC ----------

@pytest.mark.asyncio
async def test_run_fetches_and_inserts(con, monkeypatch):
    monkeypatch.setattr(et, "RPC_ENDPOINTS", [URL])
    monkeypatch.setattr(et, "EXTRACT_BATCH_SIZE", 100)
    from contextlib import contextmanager

    @contextmanager
    def fake_wh():
        yield con

    monkeypatch.setattr(et, "warehouse", fake_wh)

    # 3 sigs to fetch
    for i in range(3):
        _seed_sig(con, f"s{i}", block_time=1000 + i)

    # Helius returns 3 tx payloads (out-of-order by id)
    batch_response = [
        {"jsonrpc": "2.0", "id": 1, "result": {"blockTime": 1001, "slot": 1, "meta": {}}},
        {"jsonrpc": "2.0", "id": 0, "result": {"blockTime": 1000, "slot": 1, "meta": {}}},
        {"jsonrpc": "2.0", "id": 2, "result": {"blockTime": 1002, "slot": 1, "meta": {}}},
    ]
    with respx.mock() as mock:
        mock.post(URL).mock(return_value=httpx.Response(200, json=batch_response))
        result = await et.run()
    assert result["total"] == 3
    assert result["fetched"] == 3
    assert result["missing"] == 0

    # All three present in raw_helius_tx
    n = con.execute("SELECT COUNT(*) FROM raw_helius_tx").fetchone()[0]
    assert n == 3


@pytest.mark.asyncio
async def test_run_handles_null_results(con, monkeypatch):
    """Helius returning null for a sig (tx not found) is counted as missing, not crashed."""
    monkeypatch.setattr(et, "RPC_ENDPOINTS", [URL])
    monkeypatch.setattr(et, "EXTRACT_BATCH_SIZE", 100)
    from contextlib import contextmanager

    @contextmanager
    def fake_wh():
        yield con

    monkeypatch.setattr(et, "warehouse", fake_wh)

    _seed_sig(con, "sExists", block_time=1000)
    _seed_sig(con, "sMissing", block_time=2000)

    batch_response = [
        {"jsonrpc": "2.0", "id": 0, "result": {"blockTime": 2000, "slot": 1}},
        {"jsonrpc": "2.0", "id": 1, "result": None},  # sMissing returns null
    ]
    with respx.mock() as mock:
        mock.post(URL).mock(return_value=httpx.Response(200, json=batch_response))
        result = await et.run()
    assert result["fetched"] == 1
    assert result["missing"] == 1
    rows = con.execute("SELECT signature FROM raw_helius_tx").fetchall()
    assert len(rows) == 1


@pytest.mark.asyncio
async def test_run_skips_failed_batch_without_aborting(con, monkeypatch):
    """A batch that throws a non-retryable HTTPStatusError is logged + skipped;
    subsequent batches still run.
    """
    monkeypatch.setattr(et, "RPC_ENDPOINTS", [URL])
    monkeypatch.setattr(et, "EXTRACT_BATCH_SIZE", 2)
    from contextlib import contextmanager

    @contextmanager
    def fake_wh():
        yield con

    monkeypatch.setattr(et, "warehouse", fake_wh)

    # 4 sigs, batch size 2 = 2 batches. First batch fails with permanent 400,
    # second batch succeeds.
    for i in range(4):
        _seed_sig(con, f"s{i}", block_time=1000 + i)

    success_batch = [
        {"jsonrpc": "2.0", "id": 0, "result": {"blockTime": 1000, "slot": 1}},
        {"jsonrpc": "2.0", "id": 1, "result": {"blockTime": 1001, "slot": 1}},
    ]
    with respx.mock() as mock:
        # First call returns 400, second call returns valid batch
        mock.post(URL).mock(side_effect=[
            httpx.Response(400, text="bad request"),
            httpx.Response(200, json=success_batch),
        ])
        result = await et.run()

    # Run completed despite first batch failing
    assert result["total"] == 4
    assert result["fetched"] == 2
    assert result["failed_batches"] == 1
    # Two rows landed (from the second batch)
    assert con.execute("SELECT COUNT(*) FROM raw_helius_tx").fetchone()[0] == 2


@pytest.mark.asyncio
async def test_run_idempotent_when_nothing_missing(con, monkeypatch):
    """Re-running with all sigs already fetched is a no-op."""
    monkeypatch.setattr(et, "RPC_ENDPOINTS", [URL])
    from contextlib import contextmanager

    @contextmanager
    def fake_wh():
        yield con

    monkeypatch.setattr(et, "warehouse", fake_wh)

    _seed_sig(con, "s1")
    _seed_tx(con, "s1")

    # No HTTP calls should happen — assert_all_called with no routes
    with respx.mock(assert_all_called=False):
        result = await et.run()
    assert result == {"total": 0, "fetched": 0, "missing": 0}
