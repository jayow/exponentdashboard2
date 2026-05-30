"""Decoder for Exponent v2 XPBook orderbook accounts.

Layout reverse-engineered from @exponent-labs/exponent-fetcher@0.9.17
(deserializeOrderbook) and confirmed against the public Sec3 audit
(Nov-2025) which discloses the source tree.

The 317KB book account holds three NodeAllocator slabs:
  - PriceNode RB-tree  (MAX=1000, slot=36 bytes; key u32 = round(ln(1+APY)·1e6))
  - Offer slab         (MAX=2500, slot=40 bytes)
  - UserEscrow slab    (MAX=1500, slot=100 bytes)

Each Offer.price_ptr indexes into the PriceNode slab.
Each Offer.user_vault_ptr indexes into the UserEscrow slab.
"""
from __future__ import annotations
import math
import struct
from dataclasses import dataclass, field
from typing import Optional

import base58


# Constants from @exponent-labs/exponent-types
MAX_PRICE_NODES = 1000
MAX_OFFERS = 2500
MAX_USER_ESCROWS = 1500
PRICE_NODE_SIZE = 36
OFFER_NODE_SIZE = 40
ESCROW_NODE_SIZE = 100
ANCHOR_DISC = 8

# Discriminator for Book accounts owned by XPBook
BOOK_DISC_HEX = "eb0d265bf5722a9a"
# Discriminator for Market accounts owned by XPBook
MARKET_DISC_HEX = "0e8bd6c15e8d99ef"


@dataclass
class Offer:
    idx: int
    user_vault_ptr: int
    price_ptr: int
    amount: int        # atomic units (SY has 9 decimals)
    expiry_at: int     # unix seconds
    created_at: int
    virtual_offer: int
    type_flag: int
    fok: int
    next_ptr: int


@dataclass
class PriceNode:
    idx: int
    key: int           # round(ln(1+APY)·1e6); APY = exp(key/1e6) - 1
    first_sell: int
    first_buy: int
    last_sell: int
    last_buy: int


@dataclass
class UserEscrow:
    idx: int
    user: str          # base58 pubkey
    pt: int
    sy: int
    yt: int
    staked_yt: int
    staged: int


@dataclass
class DecodedBook:
    threshold_amount: int
    ln_maker_fee: float
    ln_taker_fee: float
    price_decimals: int
    pubkeys: list[str]            # 10 protocol-config pubkeys
    yt_bal: int
    sy_bal: int
    pt_bal: int
    yt_fee: int
    sy_fee: int
    pt_fee: int
    staged_sy: int
    expiration_ts: int            # market maturity (unix sec)
    price_root: int
    prices: dict[int, PriceNode]  # 1-indexed
    offers: list[Offer]           # only slots with user_vault_ptr != 0
    escrows: dict[int, UserEscrow]


class _Reader:
    def __init__(self, buf: bytes):
        self.buf = buf
        self.off = 0

    def u8(self) -> int:
        v = self.buf[self.off]; self.off += 1; return v

    def u32(self) -> int:
        v = struct.unpack_from("<I", self.buf, self.off)[0]; self.off += 4; return v

    def u64(self) -> int:
        v = struct.unpack_from("<Q", self.buf, self.off)[0]; self.off += 8; return v

    def i64(self) -> int:
        v = struct.unpack_from("<q", self.buf, self.off)[0]; self.off += 8; return v

    def f64(self) -> float:
        v = struct.unpack_from("<d", self.buf, self.off)[0]; self.off += 8; return v

    def pubkey(self) -> str:
        v = base58.b58encode(self.buf[self.off:self.off+32]).decode()
        self.off += 32
        return v

    def skip(self, n: int) -> None:
        self.off += n


def decode_book(buf: bytes) -> DecodedBook:
    """Decode a Book account's full state."""
    r = _Reader(buf)
    r.skip(ANCHOR_DISC)

    # ConfigurationOptions
    threshold_amount = r.u64()
    ln_maker_fee = r.f64()
    ln_taker_fee = r.f64()
    price_decimals = r.u8()
    r.skip(1135)

    pubkeys = [r.pubkey() for _ in range(10)]

    r.skip(32)  # last_sy_exchange_rate (Number = 32B)
    r.skip(32)  # OrderbookFinancials.last_seen_sy_index

    yt_bal = r.u64()
    sy_bal = r.u64()
    pt_bal = r.u64()
    yt_fee = r.u64()
    sy_fee = r.u64()
    pt_fee = r.u64()
    staged_sy = r.u64()
    expiration_ts = r.u32()
    r.skip(4)

    # PriceNode slab
    price_root = r.u32(); r.skip(12)
    r.u64(); r.u32(); r.u32()  # size, bump, free_list_head

    prices: dict[int, PriceNode] = {}
    for i in range(MAX_PRICE_NODES):
        r.u32(); r.u32(); r.u32(); r.u32()  # left right parent color
        key = r.u32()
        first_sell = r.u32(); first_buy = r.u32()
        last_sell = r.u32(); last_buy = r.u32()
        prices[i+1] = PriceNode(
            idx=i+1, key=key,
            first_sell=first_sell, first_buy=first_buy,
            last_sell=last_sell, last_buy=last_buy,
        )

    # Offer slab
    r.u64(); r.u32(); r.u32()  # size, bump, free_list_head
    offers: list[Offer] = []
    for i in range(MAX_OFFERS):
        r.u32()                     # register
        next_ptr = r.u32()
        user_vault_ptr = r.u32()
        price_ptr = r.u32()
        amount = r.u64()
        expiry_at = r.u32()
        created_at = r.u32()
        virtual_offer = r.u8()
        type_flag = r.u8()
        fok = r.u8()
        r.skip(5)
        if user_vault_ptr != 0:
            offers.append(Offer(
                idx=i+1, user_vault_ptr=user_vault_ptr, price_ptr=price_ptr,
                amount=amount, expiry_at=expiry_at, created_at=created_at,
                virtual_offer=virtual_offer, type_flag=type_flag, fok=fok,
                next_ptr=next_ptr,
            ))

    # UserEscrow slab
    r.u64(); r.u32(); r.u32()
    escrows: dict[int, UserEscrow] = {}
    NULL_PK = "11111111111111111111111111111111"
    for i in range(MAX_USER_ESCROWS):
        r.u32(); r.u32()                  # reg1 reg2
        user = base58.b58encode(r.buf[r.off:r.off+32]).decode()
        r.skip(32)
        r.skip(32)                        # yieldIndex
        pt = r.u64(); sy = r.u64(); yt = r.u64()
        staked_yt = r.i64(); staged = r.i64()
        r.skip(8)
        if user != NULL_PK:
            escrows[i+1] = UserEscrow(
                idx=i+1, user=user, pt=pt, sy=sy, yt=yt,
                staked_yt=staked_yt, staged=staged,
            )

    return DecodedBook(
        threshold_amount=threshold_amount,
        ln_maker_fee=ln_maker_fee, ln_taker_fee=ln_taker_fee,
        price_decimals=price_decimals, pubkeys=pubkeys,
        yt_bal=yt_bal, sy_bal=sy_bal, pt_bal=pt_bal,
        yt_fee=yt_fee, sy_fee=sy_fee, pt_fee=pt_fee,
        staged_sy=staged_sy, expiration_ts=expiration_ts,
        price_root=price_root, prices=prices, offers=offers, escrows=escrows,
    )


def key_to_apy(key: int) -> float:
    """PriceNode.key (u32) → APY as a fraction. key = round(ln(1+APY) · 1e6)."""
    return math.exp(key / 1e6) - 1.0 if key > 0 else 0.0


def offer_side(offer: Offer, price: PriceNode, offers_by_idx: dict[int, Offer]) -> str:
    """Walk the PriceNode's buy/sell linked lists to determine side.

    Returns 'bid' (buy YT) or 'ask' (sell YT) or '?' if the offer isn't
    reachable from either list (shouldn't happen for live offers).
    """
    # walk buys
    cur = price.first_buy
    seen = set()
    while cur and cur not in seen:
        seen.add(cur)
        if cur == offer.idx:
            return "bid"
        nxt = offers_by_idx.get(cur)
        cur = nxt.next_ptr if nxt else 0

    cur = price.first_sell
    seen.clear()
    while cur and cur not in seen:
        seen.add(cur)
        if cur == offer.idx:
            return "ask"
        nxt = offers_by_idx.get(cur)
        cur = nxt.next_ptr if nxt else 0

    return "?"


@dataclass
class FlatOrder:
    """One row per resting order — what we write to raw_v2_orders_snapshots."""
    offer_idx: int
    owner_wallet: str
    side: str        # 'bid' | 'ask' | '?'
    apy_rate: float  # decimal (0.1225 not 12.25)
    size_sy_atomic: int
    expiry_at: int
    created_at: int
    type_flag: int
    virtual_offer: int
    fok: int


def flatten_orders(book: DecodedBook) -> list[FlatOrder]:
    """Join Offers × PriceNodes × Escrows → one row per resting order."""
    offers_by_idx = {o.idx: o for o in book.offers}
    out: list[FlatOrder] = []
    for o in book.offers:
        p = book.prices.get(o.price_ptr)
        if not p or p.key == 0:
            continue
        owner = book.escrows.get(o.user_vault_ptr)
        owner_pk = owner.user if owner else ""
        out.append(FlatOrder(
            offer_idx=o.idx,
            owner_wallet=owner_pk,
            side=offer_side(o, p, offers_by_idx),
            apy_rate=key_to_apy(p.key),
            size_sy_atomic=o.amount,
            expiry_at=o.expiry_at,
            created_at=o.created_at,
            type_flag=o.type_flag,
            virtual_offer=o.virtual_offer,
            fok=o.fok,
        ))
    return out
