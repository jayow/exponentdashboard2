"""Local v2 orderbook peek tool.

Fetches an XPBook book account live from RPC, decodes its three NodeAllocator
slabs (PriceNode RB-tree, Offer slab, UserEscrow slab), and prints aggregated
views: bid/ask depth per tick, top wallets, summary stats.

Defaults to the YT-ONyc book. Pass --market <book_pubkey> for a different one.

Account layout reverse-engineered from @exponent-labs/exponent-fetcher@0.9.17.
"""
from __future__ import annotations
import argparse
import base64
import json
import math
import os
import struct
import sys
from collections import defaultdict
from pathlib import Path

import base58
import httpx
from dotenv import load_dotenv


load_dotenv()
RPC_URL = (os.getenv("SOLANA_RPC_URLS") or "").split(",")[0].strip()
if not RPC_URL:
    sys.exit("SOLANA_RPC_URLS not set in .env")

YT_ONYC_BOOK = "EkMwpJy1hm1FtCaTH7WFRkvbEXeFd5Sw4EnCpRKvv1fN"

MAX_PRICE_NODES = 1000
MAX_OFFERS = 2500
MAX_USER_ESCROWS = 1500
PRICE_NODE_SIZE = 36
OFFER_NODE_SIZE = 40
ESCROW_NODE_SIZE = 100
SIDE_BID = 512
SIDE_ASK = 256


def fetch_account(pubkey: str) -> bytes:
    r = httpx.post(
        RPC_URL,
        json={
            "jsonrpc": "2.0", "id": 1, "method": "getAccountInfo",
            "params": [pubkey, {"encoding": "base64"}],
        },
        timeout=30,
    )
    r.raise_for_status()
    res = r.json().get("result", {}).get("value")
    if not res:
        sys.exit(f"account {pubkey} not found")
    return base64.b64decode(res["data"][0])


class Reader:
    def __init__(self, buf: bytes):
        self.buf = buf
        self.off = 0

    def u8(self):
        v = self.buf[self.off]; self.off += 1; return v

    def u32(self):
        v = struct.unpack_from("<I", self.buf, self.off)[0]; self.off += 4; return v

    def u64(self):
        v = struct.unpack_from("<Q", self.buf, self.off)[0]; self.off += 8; return v

    def i64(self):
        v = struct.unpack_from("<q", self.buf, self.off)[0]; self.off += 8; return v

    def f64(self):
        v = struct.unpack_from("<d", self.buf, self.off)[0]; self.off += 8; return v

    def pubkey(self):
        v = base58.b58encode(self.buf[self.off:self.off+32]).decode()
        self.off += 32
        return v

    def skip(self, n):
        self.off += n


def decode_book(buf: bytes) -> dict:
    r = Reader(buf)
    r.skip(8)  # anchor disc

    # ConfigurationOptions
    threshold_amount = r.u64()
    ln_maker_fee = r.f64()
    ln_taker_fee = r.f64()
    price_decimals = r.u8()
    r.skip(1135)

    # 10 pubkeys
    pubkeys = [r.pubkey() for _ in range(10)]

    # last_sy_exchange_rate (32 bytes Number)
    r.skip(32)
    # OrderbookFinancials
    r.skip(32)  # last_seen_sy_index
    yt_bal = r.u64()
    sy_bal = r.u64()
    pt_bal = r.u64()
    yt_fee = r.u64()
    sy_fee = r.u64()
    pt_fee = r.u64()
    staged_sy = r.u64()
    expiration_ts = r.u32()
    r.skip(4)

    # PriceNode slab (RB tree)
    price_root = r.u32(); r.skip(12)
    price_size = r.u64(); price_bump = r.u32(); price_free = r.u32()

    prices = {}
    for i in range(MAX_PRICE_NODES):
        node_off = r.off
        left = r.u32(); right = r.u32(); parent = r.u32(); color = r.u32()
        key = r.u32()
        first_sell = r.u32(); first_buy = r.u32()
        last_sell = r.u32(); last_buy = r.u32()
        prices[i+1] = dict(
            key=key, left=left, right=right, parent=parent, color=color,
            first_sell=first_sell, first_buy=first_buy,
            last_sell=last_sell, last_buy=last_buy, byte_off=node_off,
        )

    # Offer slab
    offers_size = r.u64(); offers_bump = r.u32(); offers_free = r.u32()
    offers = []
    for i in range(MAX_OFFERS):
        node_off = r.off
        register = r.u32()
        next_ptr = r.u32()
        user_vault_ptr = r.u32()
        price_ptr = r.u32()
        amount = r.u64()
        expiry_at = r.u32()
        created_at = r.u32()
        virtual_off = r.u8()
        type_flag = r.u8()
        fok = r.u8()
        r.skip(5)
        if user_vault_ptr != 0:
            offers.append(dict(
                idx=i+1, byte_off=node_off,
                user_vault_ptr=user_vault_ptr,
                price_ptr=price_ptr,
                amount=amount,
                expiry_at=expiry_at, created_at=created_at,
                virtual_offer=virtual_off, type_flag=type_flag, fok=fok,
                next_ptr=next_ptr,
            ))

    # UserEscrow slab
    esc_size = r.u64(); esc_bump = r.u32(); esc_free = r.u32()
    escrows = {}
    for i in range(MAX_USER_ESCROWS):
        node_off = r.off
        r.u32(); r.u32()  # reg1 reg2
        user_pk = base58.b58encode(buf[r.off:r.off+32]).decode()
        r.skip(32)
        r.skip(32)  # yieldIndex
        pt = r.u64(); sy = r.u64(); yt = r.u64()
        staked_yt = r.i64(); staged = r.i64()
        r.skip(8)
        if user_pk != "11111111111111111111111111111111":  # default/empty
            escrows[i+1] = dict(user=user_pk, pt=pt, sy=sy, yt=yt,
                                staked_yt=staked_yt, staged=staged, byte_off=node_off)

    return dict(
        threshold_amount=threshold_amount,
        ln_maker_fee=ln_maker_fee, ln_taker_fee=ln_taker_fee,
        price_decimals=price_decimals,
        pubkeys=pubkeys,
        yt_bal=yt_bal, sy_bal=sy_bal, pt_bal=pt_bal,
        yt_fee=yt_fee, sy_fee=sy_fee, pt_fee=pt_fee,
        staged_sy=staged_sy, expiration_ts=expiration_ts,
        prices=prices, offers=offers, escrows=escrows,
    )


def key_to_apy(key: int) -> float:
    """key = round(ln(1+APY) * 1e6) → APY in %"""
    return (math.exp(key / 1e6) - 1) * 100 if key > 0 else 0.0


def print_orderbook(book: dict, top_n: int = 10):
    prices = book["prices"]
    offers = book["offers"]
    escrows = book["escrows"]

    # Join offers with their price level + owner wallet
    enriched = []
    for o in offers:
        p = prices.get(o["price_ptr"])
        if not p or p["key"] == 0:
            continue
        owner = escrows.get(o["user_vault_ptr"], {}).get("user", "<unknown>")
        # Determine side by tracing back through PriceNode lists.
        # An offer is reachable from first_buy/last_buy or first_sell/last_sell of its price node.
        # Simpler: check both linked lists for the offer's index.
        is_bid = False; is_ask = False
        for pn in prices.values():
            if pn["key"] == p["key"]:
                # walk buy list
                cur = pn["first_buy"]
                while cur != 0:
                    if cur == o["idx"]: is_bid = True; break
                    nxt = next((x["next_ptr"] for x in offers if x["idx"] == cur), 0)
                    if nxt == cur: break
                    cur = nxt
                cur = pn["first_sell"]
                while cur != 0:
                    if cur == o["idx"]: is_ask = True; break
                    nxt = next((x["next_ptr"] for x in offers if x["idx"] == cur), 0)
                    if nxt == cur: break
                    cur = nxt
                if is_bid or is_ask: break
        side = "BID" if is_bid else "ASK" if is_ask else "?"
        enriched.append(dict(
            idx=o["idx"], owner=owner, apy=key_to_apy(p["key"]),
            size_sy=o["amount"] / 1e9,  # 6 decimals based on price_decimals — verify
            side=side,
            created_at=o["created_at"], expiry_at=o["expiry_at"],
            virtual_offer=o["virtual_offer"], type_flag=o["type_flag"],
        ))

    bids = [e for e in enriched if e["side"] == "BID"]
    asks = [e for e in enriched if e["side"] == "ASK"]
    unknowns = [e for e in enriched if e["side"] == "?"]

    print(f"\n{'='*72}")
    print(f"Open orders:  {len(enriched)} total")
    print(f"  Bids (Buy YT):  {len(bids):>4}  ·  {sum(e['size_sy'] for e in bids):>14,.2f} SY notional")
    print(f"  Asks (Sell YT): {len(asks):>4}  ·  {sum(e['size_sy'] for e in asks):>14,.2f} SY notional")
    if unknowns:
        print(f"  (side-undetermined: {len(unknowns)})")

    bidders = {e["owner"] for e in bids}
    askers = {e["owner"] for e in asks}
    both = bidders & askers
    print(f"\nUnique wallets ({len(bidders | askers)} total):")
    print(f"  Bidders only:  {len(bidders - both):>3}")
    print(f"  Askers only:   {len(askers - both):>3}")
    print(f"  Both sides:    {len(both):>3}  (market-maker-like)")

    bid_buckets = defaultdict(list)
    for e in bids:
        bid_buckets[round(e["apy"], 4)].append(e)
    ask_buckets = defaultdict(list)
    for e in asks:
        ask_buckets[round(e["apy"], 4)].append(e)

    best_bid = max(bid_buckets) if bid_buckets else None
    best_ask = min(ask_buckets) if ask_buckets else None
    if best_bid is not None and best_ask is not None:
        spread_bps = (best_ask - best_bid) * 100
        print(f"\nBest bid: {best_bid:>6.4f}%   Best ask: {best_ask:>6.4f}%   "
              f"Spread: {spread_bps:+.1f} bps")

    # Combined ladder — asks descending on top, bids descending below
    print(f"\n{'='*78}\nORDER BOOK LADDER  (size = total SY, n = orders, w = unique wallets)")
    print(f"{'side':>5}  {'APY':>8}  {'n':>4}  {'w':>4}  {'size SY':>16}")

    for apy in sorted(ask_buckets.keys(), reverse=True):
        bucket = ask_buckets[apy]
        size_total = sum(x["size_sy"] for x in bucket)
        wallets = len({x["owner"] for x in bucket})
        print(f"  ASK  {apy:>6.4f}%  {len(bucket):>4}  {wallets:>4}  {size_total:>16,.2f}")

    print(f"  ---  ---spread---")

    for apy in sorted(bid_buckets.keys(), reverse=True):
        bucket = bid_buckets[apy]
        size_total = sum(x["size_sy"] for x in bucket)
        wallets = len({x["owner"] for x in bucket})
        print(f"  BID  {apy:>6.4f}%  {len(bucket):>4}  {wallets:>4}  {size_total:>16,.2f}")

    # Top wallets by total notional
    print(f"\n{'='*72}\nTOP {top_n} WALLETS by total notional in book")
    by_wallet = defaultdict(lambda: {"bid_sy": 0, "ask_sy": 0, "n_orders": 0})
    for e in enriched:
        slot = "bid_sy" if e["side"] == "BID" else "ask_sy" if e["side"] == "ASK" else "bid_sy"
        by_wallet[e["owner"]][slot] += e["size_sy"]
        by_wallet[e["owner"]]["n_orders"] += 1
    ranked = sorted(by_wallet.items(),
                    key=lambda kv: kv[1]["bid_sy"] + kv[1]["ask_sy"],
                    reverse=True)[:top_n]
    print(f"{'wallet':<46}  {'orders':>7}  {'bid SY':>14}  {'ask SY':>14}")
    for w, stats in ranked:
        print(f"  {w:<44}  {stats['n_orders']:>7}  {stats['bid_sy']:>14,.2f}  {stats['ask_sy']:>14,.2f}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--market", default=YT_ONYC_BOOK,
                    help=f"Book account pubkey (default: YT-ONyc = {YT_ONYC_BOOK})")
    ap.add_argument("--top", type=int, default=10,
                    help="How many top wallets to show")
    args = ap.parse_args()

    print(f"Fetching book {args.market} from {RPC_URL.split('?')[0]}…")
    buf = fetch_account(args.market)
    print(f"Book size: {len(buf):,} bytes")
    book = decode_book(buf)
    print(f"expiration_ts = {book['expiration_ts']}  "
          f"yt_bal = {book['yt_bal']:,}  sy_bal = {book['sy_bal']:,}  "
          f"pt_bal = {book['pt_bal']:,}")
    print_orderbook(book, top_n=args.top)


if __name__ == "__main__":
    main()
