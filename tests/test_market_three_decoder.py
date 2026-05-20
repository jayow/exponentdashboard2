"""Unit tests for the MarketThree binary decoder.

Builds synthetic account-data byte buffers and verifies the decoder reads
the expected pubkeys + maturity timestamp from the documented offsets.
"""
from __future__ import annotations
import struct

import base58
import pytest

from extract_load.market_three_decoder import (
    MARKET_THREE_DISCRIMINATOR,
    MarketThree,
    decode,
    find_maturity_ts,
    is_market_three,
)


def _pk(seed: int) -> bytes:
    """Synthetic 32-byte pubkey bytes (zero-padded counter)."""
    return seed.to_bytes(32, "big")


def _build_account(
    sy_seed: int = 1,
    vault_seed: int = 2,
    maturity_offset: int | None = 416,
    maturity_ts: int = 1_780_000_000,
    total_size: int = 600,
) -> bytes:
    """Build a synthetic MarketThree account.
    - sy_mint at offset 72
    - vault   at offset 104
    - maturity_ts at the given offset (None = leave zero)
    """
    buf = bytearray(total_size)
    buf[0:8] = MARKET_THREE_DISCRIMINATOR
    buf[72:104] = _pk(sy_seed)
    buf[104:136] = _pk(vault_seed)
    if maturity_offset is not None:
        struct.pack_into("<q", buf, maturity_offset, maturity_ts)
    return bytes(buf)


def test_is_market_three_true_when_discriminator_matches():
    data = _build_account()
    assert is_market_three(data) is True


def test_is_market_three_false_when_discriminator_differs():
    data = bytearray(_build_account())
    data[0] = 0  # corrupt the first byte
    assert is_market_three(bytes(data)) is False


def test_is_market_three_false_when_too_short():
    assert is_market_three(b"\x00" * 4) is False


def test_decode_returns_none_for_non_market_three():
    assert decode(b"\x00" * 600) is None


def test_decode_returns_none_when_account_too_small():
    # discriminator + only 50 bytes — can't reach offset 136
    buf = bytearray(50)
    buf[0:8] = MARKET_THREE_DISCRIMINATOR
    assert decode(bytes(buf)) is None


def test_decode_extracts_sy_and_vault():
    data = _build_account(sy_seed=42, vault_seed=99)
    decoded = decode(data)
    assert decoded is not None
    assert decoded.sy_mint == base58.b58encode(_pk(42)).decode()
    assert decoded.vault == base58.b58encode(_pk(99)).decode()


def test_decode_finds_maturity_at_416():
    data = _build_account(maturity_offset=416, maturity_ts=1_780_000_000)
    decoded = decode(data)
    assert decoded.maturity_ts == 1_780_000_000


def test_decode_finds_maturity_at_fallback_offset_260():
    """If 416 is empty but 260 has a plausible timestamp, we should find it."""
    data = _build_account(maturity_offset=260, maturity_ts=1_785_000_000)
    decoded = decode(data)
    assert decoded.maturity_ts == 1_785_000_000


def test_decode_maturity_none_when_no_plausible_ts():
    """All offsets are zero — no maturity found."""
    data = _build_account(maturity_offset=None)
    decoded = decode(data)
    assert decoded is not None
    assert decoded.maturity_ts is None


def test_find_maturity_ts_rejects_out_of_range():
    """Implausibly old (epoch 0) or far-future timestamps must be rejected."""
    buf = bytearray(600)
    buf[0:8] = MARKET_THREE_DISCRIMINATOR
    struct.pack_into("<q", buf, 416, 0)  # epoch 0
    struct.pack_into("<q", buf, 260, 2_500_000_000)  # year 2049
    # Neither should be accepted
    assert find_maturity_ts(bytes(buf)) is None


def test_decode_prefers_offset_364_over_416_when_both_set():
    """Regression for the silent-wrong-maturity bug: prior order tried 416
    first, so a created_at-style timestamp at 416 hid the real maturity_ts
    at 364. Now 364 must win when both are populated."""
    buf = bytearray(_build_account(maturity_offset=416, maturity_ts=1_750_000_000))
    # Plant the real maturity_ts at 364
    struct.pack_into("<q", buf, 364, 1_786_000_000)
    decoded = decode(bytes(buf))
    assert decoded.maturity_ts == 1_786_000_000


def test_find_maturity_ts_brute_scan_fallback():
    """If none of the canonical offsets work, the scan from 104..500 step 8 finds one."""
    buf = bytearray(600)
    buf[0:8] = MARKET_THREE_DISCRIMINATOR
    # Plant a plausible ts at offset 280 (not in canonical list)
    struct.pack_into("<q", buf, 280, 1_790_000_000)
    assert find_maturity_ts(bytes(buf)) == 1_790_000_000
