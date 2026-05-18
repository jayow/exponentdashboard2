"""Pure binary decoder for Exponent's MarketThree on-chain account.

MarketThree layout is Anchor-style: 8-byte discriminator + struct.
Without the IDL we use empirically-discovered byte offsets (same approach
as v1/src/discover_expired_markets.py). When offsets change, this file is
the single place to update — keeps the decode logic isolated.

Offset map (verified against active markets in v1):
  [0..8)    Anchor discriminator (must match MARKET_THREE_DISCRIMINATOR)
  [8..40)   <other pubkey — purpose TBD>
  [40..72)  <other pubkey>
  [72..104) SY mint                                ← key field
  [104..136) Vault PDA (holds the underlying)      ← key field

  Maturity timestamp (i64 LE) lives at one of these offsets, in order of
  observed frequency: 416 / 364 / 312 / 260 / 208. We probe each and
  validate the value is plausible (after 2023, before 2030). Fallback:
  scan i64-aligned offsets for the first plausible timestamp.

This decoder is pure: takes account-data bytes, returns a dict. No I/O.
"""
from __future__ import annotations
import struct
from dataclasses import dataclass

import base58


MARKET_THREE_DISCRIMINATOR_HEX = "d404847ea9797914"
MARKET_THREE_DISCRIMINATOR = bytes.fromhex(MARKET_THREE_DISCRIMINATOR_HEX)

# i64 LE bounds for plausibility (2023-11 to 2030-01)
TS_MIN = 1_700_000_000
TS_MAX = 1_900_000_000

# Tried in order; first plausible wins.
MATURITY_TS_CANDIDATE_OFFSETS = (416, 364, 312, 260, 208)


@dataclass
class MarketThree:
    sy_mint: str
    vault: str
    maturity_ts: int | None
    raw_size: int


def _decode_pubkey(data: bytes, offset: int) -> str:
    return base58.b58encode(data[offset : offset + 32]).decode()


def _read_i64_le(data: bytes, offset: int) -> int | None:
    if offset + 8 > len(data):
        return None
    return struct.unpack("<q", data[offset : offset + 8])[0]


def find_maturity_ts(data: bytes) -> int | None:
    """Scan known offsets first, then fall back to brute aligned scan."""
    for offset in MATURITY_TS_CANDIDATE_OFFSETS:
        ts = _read_i64_le(data, offset)
        if ts is not None and TS_MIN < ts < TS_MAX:
            return ts
    # Fallback: scan i64-aligned offsets from past the discriminator
    for offset in range(104, min(len(data) - 7, 500), 8):
        ts = _read_i64_le(data, offset)
        if ts is not None and TS_MIN < ts < TS_MAX:
            return ts
    return None


def is_market_three(data: bytes) -> bool:
    return len(data) >= 8 and data[:8] == MARKET_THREE_DISCRIMINATOR


def decode(data: bytes) -> MarketThree | None:
    """Decode a MarketThree account. Returns None if discriminator doesn't match
    or the account is too short to contain key fields.
    """
    if not is_market_three(data):
        return None
    if len(data) < 136:
        return None
    return MarketThree(
        sy_mint=_decode_pubkey(data, 72),
        vault=_decode_pubkey(data, 104),
        maturity_ts=find_maturity_ts(data),
        raw_size=len(data),
    )
