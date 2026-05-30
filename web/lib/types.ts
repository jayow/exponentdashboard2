export type MarketMeta = {
  key: string;
  ticker: string;
  platform: string;
  maturity: string;
  tvl: number;
};

export type WalletRow = {
  addr: string;
  farm:   { buyYt: number; sellYt: number; claimYield: number };
  lp:     { addLiq: number; removeLiq: number };
  income: { buyPt: number; sellPt: number; strip: number; redeemPt: number };
  byMarket: Record<string, number>;
  totalVolume: number;
  farmNet: number;
  lpNet: number;
  txs: number;
};

export type Dataset = {
  generatedAt: string;
  markets: MarketMeta[];
  totals: {
    wallets: number;
    markets: number;
    farmBuys: number;
    farmSells: number;
    farmClaims: number;
    lpAdds: number;
    lpRemoves: number;
  };
  wallets: WalletRow[];
};

export type TradeEvent = {
  sig: string;
  blockTime: number;
  market: string;
  signer: string;
  action: string;
  instr?: string;
  ytDelta: number;
  underlyingDelta: number;
  usdNet: number;
};

// v2 (XPBook) orderbook types — match serve/build_web_data.py output
export type V2OrderRow = {
  owner: string;
  apy: number;            // decimal, e.g. 0.1225
  sizeSy: number;
  sizeSyAtomic: string;
  createdAt: number;      // unix sec
  expiryAt: number;
  virtualOffer: number;
};

export type V2Book = {
  bookAccount: string;
  nOffers: number;
  nBids: number;
  nAsks: number;
  bestBidApy: number | null;
  bestAskApy: number | null;
  bids: V2OrderRow[];     // sorted apy desc (best bid first)
  asks: V2OrderRow[];     // sorted apy asc  (best ask first)
};

export type V2MarketBook = {
  ticker: string;
  platform: string;
  maturityDate: string | null;
  syMint: string | null;
  primaryBookAccount: string;
  bookCount: number;
  nOffers: number;
  nBids: number;
  nAsks: number;
  bestBidApy: number | null;
  bestAskApy: number | null;
  midApy: number | null;
  spreadBps: number | null;
  totalBidSizeSy: number;
  totalAskSizeSy: number;
  books: V2Book[];
};

export type V2OrderbookData = {
  meta: { generatedAt: string; snapshotDate: string | null; marketCount: number };
  byMarket: Record<string, V2MarketBook>;
};
