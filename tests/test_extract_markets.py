"""End-to-end tests for extract_markets: mock API + mock Helius, real DuckDB."""
from __future__ import annotations
import base64
import json
import struct

import base58
import duckdb
import httpx
import pytest
import respx

from extract_load import extract_markets as em
from extract_load.helius_client import HeliusClient
from extract_load.load import RAW_DDL
from extract_load.market_three_decoder import MARKET_THREE_DISCRIMINATOR


HELIUS_URL = "https://mainnet.helius-rpc.com/?api-key=K1"


@pytest.fixture
def con():
    c = duckdb.connect(":memory:")
    for ddl in RAW_DDL.values():
        c.execute(ddl)
    try:
        yield c
    finally:
        c.close()


def _pk(seed: int) -> str:
    return base58.b58encode(seed.to_bytes(32, "big")).decode()


def _build_account_b64(sy_seed: int, vault_seed: int, maturity_ts: int) -> str:
    buf = bytearray(600)
    buf[0:8] = MARKET_THREE_DISCRIMINATOR
    buf[72:104] = sy_seed.to_bytes(32, "big")
    buf[104:136] = vault_seed.to_bytes(32, "big")
    struct.pack_into("<q", buf, 416, maturity_ts)
    return base64.b64encode(bytes(buf)).decode()


# ---------- format_market_key ----------

def test_format_market_key():
    # 15 Dec 2026 UTC
    ts = 1_797_292_800
    assert em.format_market_key("fragSOL", ts) == "fragSOL-15DEC26"


def test_format_market_key_zero_pad_day():
    # 5 Jan 2026 UTC
    ts = 1_767_571_200
    assert em.format_market_key("USDC", ts) == "USDC-05JAN26"


# ---------- api_market_to_row ----------

def test_api_market_to_row_happy_path():
    m = {
        "syMint": _pk(1),
        "vaultAddress": _pk(2),
        "ptMint": _pk(3),
        "ytMint": _pk(4),
        "underlyingAsset": {"ticker": "fragSOL", "mint": _pk(5)},
        "maturityDateUnixTs": 1_797_292_800,
        "decimals": 9,
        "platformName": "Fragmetric",
        "marketStatus": "active",
    }
    row = em.api_market_to_row(m)
    assert row is not None
    key, payload = row
    assert key == "fragSOL-15DEC26"
    assert payload["syMint"] == _pk(1)
    assert payload["platform"] == "Fragmetric"
    assert payload["maturityDate"] == "2026-12-15"


def test_api_market_to_row_returns_none_when_missing_fields():
    # No syMint
    assert em.api_market_to_row({"maturityDateUnixTs": 1, "underlyingAsset": {"ticker": "X"}}) is None
    # No ticker
    assert em.api_market_to_row({"syMint": _pk(1), "maturityDateUnixTs": 1, "underlyingAsset": {}}) is None
    # No maturity
    assert em.api_market_to_row({"syMint": _pk(1), "underlyingAsset": {"ticker": "X"}}) is None


# ---------- onchain_account_to_row ----------

def test_onchain_account_to_row_uses_ticker_from_lookup():
    data_b64 = _build_account_b64(sy_seed=100, vault_seed=200, maturity_ts=1_797_292_800)
    ticker_by_sy = {_pk(100): "fragSOL"}
    row = em.onchain_account_to_row(_pk(999), data_b64, ticker_by_sy)
    assert row is not None
    key, payload = row
    assert key == "fragSOL-15DEC26"
    assert payload["syMint"] == _pk(100)
    assert payload["vault"] == _pk(200)
    assert payload["marketAccount"] == _pk(999)


def test_onchain_account_to_row_unknown_ticker_falls_back():
    data_b64 = _build_account_b64(sy_seed=100, vault_seed=200, maturity_ts=1_797_292_800)
    row = em.onchain_account_to_row(_pk(999), data_b64, ticker_by_sy={})
    assert row is not None
    key, _payload = row
    assert key.startswith("UNKNOWN-")


def test_onchain_account_to_row_returns_none_on_bad_data():
    bad_b64 = base64.b64encode(b"\x00" * 50).decode()  # missing discriminator
    assert em.onchain_account_to_row(_pk(1), bad_b64, {}) is None


# ---------- upsert_market ----------

def test_upsert_market_inserts_then_updates(con):
    em.upsert_market(con, "fragSOL-15DEC26", "api", {"v": 1})
    em.upsert_market(con, "fragSOL-15DEC26", "api", {"v": 2})  # update
    em.upsert_market(con, "fragSOL-15DEC26", "onchain", {"v": "x"})  # different source
    rows = con.execute(
        "SELECT source, payload FROM raw_markets WHERE market_key='fragSOL-15DEC26' ORDER BY source"
    ).fetchall()
    assert len(rows) == 2
    assert rows[0][0] == "api"
    assert json.loads(rows[0][1])["v"] == 2  # updated, not duplicated
    assert rows[1][0] == "onchain"


# ---------- run() end-to-end with mocks ----------

@pytest.mark.asyncio
async def test_run_e2e_mocked(con, monkeypatch):
    # Force HELIUS_KEYS = ['K1'] for the duration of the test
    monkeypatch.setattr(em, "HELIUS_KEYS", ["K1"])

    # Patch warehouse() to yield our in-memory con
    from contextlib import contextmanager

    @contextmanager
    def fake_wh():
        yield con

    monkeypatch.setattr(em, "warehouse", fake_wh)

    api_payload = [
        {
            "syMint": _pk(1),
            "vaultAddress": _pk(2),
            "ptMint": _pk(3),
            "ytMint": _pk(4),
            "underlyingAsset": {"ticker": "fragSOL", "mint": _pk(5)},
            "maturityDateUnixTs": 1_797_292_800,
            "decimals": 9,
            "platformName": "Fragmetric",
            "marketStatus": "active",
        }
    ]

    # On-chain: one matching account (same SY mint as API) + one new
    onchain_payload = {
        "jsonrpc": "2.0",
        "id": 1,
        "result": [
            {
                "pubkey": _pk(900),
                "account": {"data": [_build_account_b64(1, 2, 1_797_292_800), "base64"]},
            },
            {
                "pubkey": _pk(901),
                "account": {"data": [_build_account_b64(77, 78, 1_780_000_000), "base64"]},  # unknown SY
            },
        ],
    }

    with respx.mock(assert_all_called=True) as mock:
        mock.get(em.EXPONENT_API_URL).mock(return_value=httpx.Response(200, json=api_payload))
        mock.post(HELIUS_URL).mock(return_value=httpx.Response(200, json=onchain_payload))
        result = await em.run()

    assert result["api"] == 1
    # Two on-chain rows: one labeled fragSOL via API ticker lookup, one UNKNOWN
    assert result["onchain"] == 2

    rows = con.execute(
        "SELECT market_key, source FROM raw_markets ORDER BY market_key, source"
    ).fetchall()
    keys = [r[0] for r in rows]
    assert "fragSOL-15DEC26" in keys
    # The unknown SY mint produces an UNKNOWN-... key
    assert any(k.startswith("UNKNOWN-") for k in keys)
