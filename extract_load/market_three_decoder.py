"""Pure binary decoder for Exponent's MarketThree on-chain account.

MarketThree layout is Anchor-style: 8-byte discriminator + struct.
Without the IDL we use empirically-discovered byte offsets (same approach
as v1/src/discover_expired_markets.py). When offsets change, this file is
the single place to update — keeps the decode logic isolated.

Offset map (verified against active markets in v1):
  [0..8)    Anchor discriminator (must match MARKET_THREE_DISCRIMINATOR)
  [8..40)   <other pubkey — purpose TBD>
  [40..72)  PT mint                                ← key field
  [72..104) SY mint                                ← key field
  [104..136) Vault PDA (holds the underlying)      ← key field
  [136..168) LP mint                               ← key field

  Maturity timestamp (i64 LE) lives at offset 364 for every active market
  account we've seen (verified 9/9 by cross-referencing the API's maturityTs).
  Older expired accounts may use other offsets — we probe them as fallbacks.
  Each candidate value must be plausible (after 2023, before 2030).
  Final fallback: scan i64-aligned offsets for the first plausible timestamp
  — used only when none of the known offsets contain a valid value.

  Why 364 first: the prior order (416, 364, …) caused the decoder to
  silently latch onto the wrong i64 field (likely created_at) for several
  current markets, mis-deriving the market_key. Empirically 364 is the
  correct field for every active MarketThree.

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
MATURITY_TS_CANDIDATE_OFFSETS = (364, 416, 312, 260, 208)


@dataclass
class MarketThree:
    sy_mint: str
    vault: str
    pt_mint: str
    lp_mint: str
    maturity_ts: int | None
    raw_size: int
    # Pool reserves (raw u64 atom units, stored in MarketTwo.financials).
    # Use these to compute Exponent's UI 'Liquidity' exactly via the
    # liquidity_pool_tvl formula:
    #   sy_balance × sy_exchange_rate + pt_balance / exp(last_ln_implied × yrs)
    pt_balance: int | None = None
    sy_balance: int | None = None
    # Time-curve params for computing pt_exchange_rate at any timestamp.
    last_ln_implied_rate: float | None = None


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

    Reserve/time-curve fields (pt_balance, sy_balance, last_ln_implied_rate)
    live in MarketTwo.financials starting at offset 364 — only present in
    full-size accounts. Older partial-layout variants return them as None.
    """
    if not is_market_three(data):
        return None
    if len(data) < 168:
        return None
    pt_bal = sy_bal = None
    last_ln = None
    maturity_ts = find_maturity_ts(data)
    if maturity_ts is not None and maturity_ts == _read_i64_le(data, 364) and len(data) >= 404:
        # Standard MarketTwo financials layout — offset 364 expiration_ts,
        # 372 pt_balance, 380 sy_balance, 388 ln_fee_rate, 396 last_ln_implied
        pt_bal = struct.unpack("<Q", data[372:380])[0]
        sy_bal = struct.unpack("<Q", data[380:388])[0]
        last_ln = struct.unpack("<d", data[396:404])[0]
    return MarketThree(
        pt_mint=_decode_pubkey(data, 40),
        sy_mint=_decode_pubkey(data, 72),
        vault=_decode_pubkey(data, 104),
        lp_mint=_decode_pubkey(data, 136),
        maturity_ts=maturity_ts,
        raw_size=len(data),
        pt_balance=pt_bal,
        sy_balance=sy_bal,
        last_ln_implied_rate=last_ln,
    )
