'use client';
import { Suspense, useEffect, useMemo, useState } from 'react';
import Link from 'next/link';
import { useSearchParams } from 'next/navigation';
import {
  AreaChart, Area, BarChart, Bar, ComposedChart, Line, XAxis, YAxis, Tooltip, ResponsiveContainer,
} from 'recharts';
import type { V2OrderbookData, V2MarketBook, V2Book, V2OrderRow } from '@/lib/types';

type Holder = { owner: string; balance: number; usd: number; sharePct: number };
type Snapshot = {
  market: string; leg: string; holders: number;
  totalBalance: number; totalUsd: number; top: Holder[];
};
type MarketHoldersData = {
  meta: any;
  byMarketLeg: Record<string, Snapshot>;
  historyByMarket?: Record<string, { dates: string[]; PT: number[]; YT: number[]; LP: number[] }>;
};

type TvlByMarket = Record<string, {
  ticker: string; tvlUsd: number[]; tvlUnderlying: number[]; principalUsd?: number[];
  ptUsd?: number[]; ytUsd?: number[]; lpUsd?: number[]; idleUsd?: number[];
}>;
type ApLeg = { byMarket: Record<string, number[]>; totals: number[] };
type ApTicker = { underlyingMint: string; latest: Record<string, number>; legs: Record<string, ApLeg> };

type Volume = { dates: string[]; byMarket?: Record<string, {
  ticker: string;
  ptUsd?: number[]; ytUsd?: number[]; totalUsd?: number[];
  ptBuyUsd?: number[]; ptSellUsd?: number[];
  ytBuyUsd?: number[]; ytSellUsd?: number[];
}> };

type Range = '30d' | '90d' | '1y' | 'all';
type Metric = 'tvl' | 'breakdown' | 'positions' | 'volume' | 'holders' | 'orderbook';
type Leg = 'PT' | 'YT' | 'LP';

const BREAKDOWN_COLOR = {
  'Principal (PT)': '#a78bfa',
  'Farm (YT)':      '#fb923c',
  'Liquidity (LP)': '#4ade80',
  'Idle':           '#9ca3af',
} as const;

function fmtUsd(n: number) {
  if (!n) return '$0';
  if (n >= 1e9) return `$${(n / 1e9).toFixed(2)}B`;
  if (n >= 1e6) return `$${(n / 1e6).toFixed(2)}M`;
  if (n >= 1e3) return `$${(n / 1e3).toFixed(1)}K`;
  return `$${n.toFixed(2)}`;
}
function fmtCount(n: number, ticker: string) {
  if (Math.abs(n) >= 1e9) return `${(n / 1e9).toFixed(2)}B ${ticker}`;
  if (Math.abs(n) >= 1e6) return `${(n / 1e6).toFixed(2)}M ${ticker}`;
  if (Math.abs(n) >= 1e3) return `${(n / 1e3).toFixed(1)}K ${ticker}`;
  return `${n.toFixed(2)} ${ticker}`;
}
function rangeStart(dates: string[], range: Range) {
  if (range === 'all') return 0;
  const days = range === '30d' ? 30 : range === '90d' ? 90 : 365;
  const cutoff = new Date(new Date(dates[dates.length - 1]).getTime() - days * 86400_000).toISOString().slice(0, 10);
  return Math.max(0, dates.findIndex(d => d >= cutoff));
}

function MarketDetailView() {
  const sp = useSearchParams();
  const marketKey = sp.get('key') || '';
  const [ticker, maturityStr] = marketKey.split('-');

  const [holders, setHolders] = useState<MarketHoldersData | null>(null);
  const [tvl, setTvl] = useState<{ dates: string[]; byMarket: TvlByMarket } | null>(null);
  const [positions, setPositions] = useState<{ dates: string[]; byTicker: Record<string, ApTicker> } | null>(null);
  const [volume, setVolume] = useState<Volume | null>(null);
  const [v2, setV2] = useState<V2OrderbookData | null>(null);
  const [activeBookIdx, setActiveBookIdx] = useState<number>(0);
  const [err, setErr] = useState<string | null>(null);

  const [tab, setTab] = useState<Leg>('PT');
  const [metric, setMetric] = useState<Metric>('tvl');
  const [range, setRange] = useState<Range>('90d');
  // Volume tab — toggle bars between absolute USD and per-day share %.
  const [volumeShare, setVolumeShare] = useState<boolean>(false);
  const [search, setSearch] = useState('');

  useEffect(() => {
    Promise.all([
      fetch('/market_holders.json').then(r => r.json()),
      fetch('/tvl.json').then(r => r.json()),
      fetch('/active_positions.json').then(r => r.json()),
      fetch('/volume.json').then(r => r.json()),
      fetch('/v2_orderbook.json').then(r => r.json()).catch(() => null),
    ])
      .then(([h, t, p, v, ob]) => {
        setHolders(h); setTvl(t); setPositions(p); setVolume(v); setV2(ob);
      })
      .catch(e => setErr(String(e)));
  }, []);

  const v2Market: V2MarketBook | null = v2?.byMarket[marketKey] ?? null;
  const v2SnapshotDate = v2?.meta.snapshotDate ?? null;

  const tvlSeries = tvl?.byMarket?.[marketKey]?.tvlUsd ?? [];
  const tickerData = positions?.byTicker?.[ticker];
  const chartData = useMemo(() => {
    if (!tvl) return [];
    const start = rangeStart(tvl.dates, range);
    const dates = tvl.dates.slice(start);
    if (metric === 'tvl') {
      return dates.map((d, i) => ({ date: d, TVL: tvlSeries[start + i] || 0 }));
    }
    if (metric === 'breakdown') {
      const m = tvl.byMarket?.[marketKey];
      const pt = m?.ptUsd ?? [], yt = m?.ytUsd ?? [], lp = m?.lpUsd ?? [], idle = m?.idleUsd ?? [];
      return dates.map((d, i) => ({
        date: d,
        'Principal (PT)': pt[start + i] || 0,
        'Farm (YT)':      yt[start + i] || 0,
        'Liquidity (LP)': lp[start + i] || 0,
        'Idle':           idle[start + i] || 0,
      }));
    }
    if (metric === 'positions' && tickerData) {
      return dates.map((d, i) => ({
        date: d,
        PT: tickerData.legs.PT?.byMarket?.[marketKey]?.[start + i] || 0,
        YT: tickerData.legs.YT?.byMarket?.[marketKey]?.[start + i] || 0,
        LP: tickerData.legs.LP?.byMarket?.[marketKey]?.[start + i] || 0,
      }));
    }
    if (metric === 'holders' && holders?.historyByMarket?.[marketKey]) {
      const h = holders.historyByMarket[marketKey];
      const hstart = rangeStart(h.dates, range);
      return h.dates.slice(hstart).map((d, i) => ({
        date: d,
        PT: h.PT[hstart + i] || 0,
        YT: h.YT[hstart + i] || 0,
        LP: h.LP[hstart + i] || 0,
      }));
    }
    if (metric === 'volume' && volume) {
      const vstart = rangeStart(volume.dates, range);
      const vdates = volume.dates.slice(vstart);
      const m = volume.byMarket?.[marketKey];
      const ptBuy  = m?.ptBuyUsd  ?? [];
      const ptSell = m?.ptSellUsd ?? [];
      const ytBuy  = m?.ytBuyUsd  ?? [];
      const ytSell = m?.ytSellUsd ?? [];
      const totals = m?.totalUsd  ?? [];
      // Cumulative over the FULL series so the line stays accurate to each
      // visible date even when zoomed in. Always absolute USD; share% only
      // applies to the bars via Recharts' stackOffset="expand".
      let run = 0;
      const cumulativeFull = totals.map(v => (run += v || 0));
      return vdates.map((d, i) => {
        const idx = vstart + i;
        return {
          date: d,
          'PT buy':  ptBuy[idx]  || 0,
          'PT sell': ptSell[idx] || 0,
          'YT buy':  ytBuy[idx]  || 0,
          'YT sell': ytSell[idx] || 0,
          Cumulative: cumulativeFull[idx] || 0,
        };
      });
    }
    return [];
  }, [tvl, positions, volume, holders, metric, range, marketKey, ticker, tvlSeries, tickerData]);

  if (err) return <div className="mx-auto max-w-[1400px] px-4 py-10 text-red-400">Error: {err}</div>;
  if (!holders || !tvl || !positions) return <div className="mx-auto max-w-[1400px] px-4 py-10 text-white/50">Loading…</div>;

  const ptSnap = holders.byMarketLeg[`${marketKey}:PT`];
  const ytSnap = holders.byMarketLeg[`${marketKey}:YT`];
  const lpSnap = holders.byMarketLeg[`${marketKey}:LP`];
  const cur = tab === 'PT' ? ptSnap : tab === 'YT' ? ytSnap : lpSnap;

  const latestSupply = tickerData?.legs[tab]?.byMarket?.[marketKey] ?? [];
  const supply = latestSupply.length ? latestSupply[latestSupply.length - 1] : 0;
  const currentTvl = tvlSeries.length ? tvlSeries[tvlSeries.length - 1] : 0;

  return (
    <main className="mx-auto max-w-[1400px] px-4 sm:px-6 py-10">
      <Link href="/" className="text-white/40 hover:text-white text-sm">← back</Link>
      <div className="mt-4 flex items-baseline gap-4 flex-wrap">
        <h1 className="text-2xl font-semibold text-white">{marketKey}</h1>
        <span className="text-sm text-white/40">{ticker} · matures {maturityStr}</span>
      </div>

      {/* Stats row */}
      <div className="mt-6 grid grid-cols-2 md:grid-cols-4 lg:grid-cols-7 gap-3 text-sm">
        <Stat label="TVL" value={fmtUsd(currentTvl)} />
        <Stat label="PT supply" value={fmtCount(tickerData?.legs.PT?.byMarket?.[marketKey]?.slice(-1)[0] || 0, ticker)} />
        <Stat label="YT supply" value={fmtCount(tickerData?.legs.YT?.byMarket?.[marketKey]?.slice(-1)[0] || 0, ticker)} />
        <Stat label="LP supply" value={fmtCount(tickerData?.legs.LP?.byMarket?.[marketKey]?.slice(-1)[0] || 0, ticker)} />
        <Stat label="PT holders" value={ptSnap ? ptSnap.holders.toLocaleString() : '–'} />
        <Stat label="YT holders" value={ytSnap ? ytSnap.holders.toLocaleString() : '–'} />
        <Stat label="LP holders" value={lpSnap ? lpSnap.holders.toLocaleString() : '–'} />
      </div>

      {/* Chart */}
      <div className="mt-8">
        <div className="flex items-center justify-between mb-3 flex-wrap gap-3">
          <div className="flex items-center gap-1">
            {(['tvl', 'breakdown', 'positions', 'volume', 'holders', 'orderbook'] as Metric[]).map(m => {
              if (m === 'orderbook' && !v2Market) return null;
              const label = m === 'tvl' ? 'TVL'
                : m === 'breakdown' ? 'Breakdown'
                : m === 'positions' ? 'Positions'
                : m === 'volume' ? 'Volume'
                : m === 'holders' ? 'Holders'
                : 'Orderbook';
              return (
                <button key={m} onClick={() => setMetric(m)}
                  className={`text-xs px-3 py-1 rounded-lg border ${metric === m ? 'border-white/30 bg-white/10 text-white' : 'border-white/10 text-white/40'}`}>
                  {label}
                  {m === 'orderbook' && v2Market && (
                    <span className="ml-1 text-emerald-300/70">·{v2Market.nOffers}</span>
                  )}
                </button>
              );
            })}
          </div>
          <div className="flex items-center gap-1">
            {metric === 'volume' && (
              <button onClick={() => setVolumeShare(v => !v)}
                className={`text-[11px] px-2.5 py-1 rounded-md border transition mr-1 ${
                  volumeShare
                    ? 'border-white/30 bg-white/10 text-white'
                    : 'border-white/10 text-white/30 hover:text-white/60'
                }`}
                title="Toggle Share % stacked view">
                Share %
              </button>
            )}
            {(['30d', '90d', '1y', 'all'] as Range[]).map(r => (
              <button key={r} onClick={() => setRange(r)}
                className={`text-xs px-2.5 py-1 rounded-md ${range === r ? 'bg-white/10 text-white' : 'text-white/30 hover:text-white/60'}`}>
                {r === 'all' ? 'All' : r.toUpperCase()}
              </button>
            ))}
          </div>
        </div>
        <div className="rounded-2xl border border-white/10 bg-white/5 p-4">
          {metric === 'orderbook' && v2Market ? (
            <OrderbookView
              market={v2Market}
              snapshotDate={v2SnapshotDate}
              activeBookIdx={activeBookIdx}
              setActiveBookIdx={setActiveBookIdx}
            />
          ) : (
          <ResponsiveContainer width="100%" height={300}>
            {metric === 'positions' ? (
              <AreaChart data={chartData as any} margin={{ top: 10, right: 10, left: 0, bottom: 0 }}>
                <XAxis dataKey="date" tick={{ fill: '#888', fontSize: 11 }} />
                <YAxis tick={{ fill: '#888', fontSize: 11 }} tickFormatter={n => fmtCount(n, '').trim()} />
                <Tooltip contentStyle={{ background: '#0a0a0a', border: '1px solid rgba(255,255,255,0.15)', fontSize: 11 }} formatter={(v: any) => fmtCount(Number(v), ticker)} />
                <Area type="monotone" dataKey="PT" stroke="#a78bfa" fill="#a78bfa66" />
                <Area type="monotone" dataKey="YT" stroke="#38bdf8" fill="#38bdf866" />
                <Area type="monotone" dataKey="LP" stroke="#4ade80" fill="#4ade8066" />
              </AreaChart>
            ) : metric === 'holders' ? (
              <ComposedChart data={chartData as any} margin={{ top: 10, right: 10, left: 0, bottom: 0 }}>
                <XAxis dataKey="date" tick={{ fill: '#888', fontSize: 11 }} />
                <YAxis tick={{ fill: '#888', fontSize: 11 }} tickFormatter={n => n.toLocaleString()} />
                <Tooltip contentStyle={{ background: '#0a0a0a', border: '1px solid rgba(255,255,255,0.15)', fontSize: 11 }} formatter={(v: any) => Number(v).toLocaleString()} />
                <Line type="monotone" dataKey="PT" stroke="#a78bfa" strokeWidth={1.5} dot={false} isAnimationActive={false} />
                <Line type="monotone" dataKey="YT" stroke="#38bdf8" strokeWidth={1.5} dot={false} isAnimationActive={false} />
                <Line type="monotone" dataKey="LP" stroke="#4ade80" strokeWidth={1.5} dot={false} isAnimationActive={false} />
              </ComposedChart>
            ) : metric === 'breakdown' ? (
              <BarChart data={chartData as any} margin={{ top: 10, right: 10, left: 0, bottom: 0 }}>
                <XAxis dataKey="date" tick={{ fill: '#888', fontSize: 11 }} />
                <YAxis tick={{ fill: '#888', fontSize: 11 }} tickFormatter={fmtUsd} />
                <Tooltip contentStyle={{ background: '#0a0a0a', border: '1px solid rgba(255,255,255,0.15)', fontSize: 11 }} formatter={(v: any) => fmtUsd(Number(v))} />
                <Bar dataKey="Principal (PT)" stackId="b" fill={BREAKDOWN_COLOR['Principal (PT)']} fillOpacity={0.9} isAnimationActive={false} />
                <Bar dataKey="Farm (YT)"      stackId="b" fill={BREAKDOWN_COLOR['Farm (YT)']}      fillOpacity={0.9} isAnimationActive={false} />
                <Bar dataKey="Liquidity (LP)" stackId="b" fill={BREAKDOWN_COLOR['Liquidity (LP)']} fillOpacity={0.9} isAnimationActive={false} />
                <Bar dataKey="Idle"           stackId="b" fill={BREAKDOWN_COLOR['Idle']}           fillOpacity={0.9} isAnimationActive={false} />
              </BarChart>
            ) : metric === 'volume' ? (
              // 4-bucket stacked bars + cumulative trajectory on a 2nd axis.
              // stackOffset="expand" pegs each day's stack to 100%; bars stay
              // absolute USD otherwise. Recharts emits values in [0,1] under
              // expand mode, so the bar Y axis formats × 100 as %.
              <ComposedChart data={chartData as any}
                             stackOffset={volumeShare ? 'expand' : 'none'}
                             margin={{ top: 10, right: 10, left: 0, bottom: 0 }}>
                <XAxis dataKey="date" tick={{ fill: '#888', fontSize: 11 }} />
                <YAxis yAxisId="bar"
                       tick={{ fill: '#888', fontSize: 11 }}
                       tickFormatter={volumeShare ? (v => `${(v * 100).toFixed(0)}%`) : fmtUsd} />
                <YAxis yAxisId="line" orientation="right"
                       tick={{ fill: '#fbbf24', fontSize: 11 }}
                       tickFormatter={fmtUsd} width={70} />
                <Tooltip contentStyle={{ background: '#0a0a0a', border: '1px solid rgba(255,255,255,0.15)', fontSize: 11 }}
                         formatter={(v: any, name: any) => name === 'Cumulative'
                           ? fmtUsd(Number(v))
                           : fmtUsd(Number(v))} />
                <Bar yAxisId="bar" dataKey="PT buy"  stackId="v" fill="#a78bfa" fillOpacity={0.9} isAnimationActive={false} />
                <Bar yAxisId="bar" dataKey="PT sell" stackId="v" fill="#f87171" fillOpacity={0.9} isAnimationActive={false} />
                <Bar yAxisId="bar" dataKey="YT buy"  stackId="v" fill="#4ade80" fillOpacity={0.9} isAnimationActive={false} />
                <Bar yAxisId="bar" dataKey="YT sell" stackId="v" fill="#fb923c" fillOpacity={0.9} isAnimationActive={false} />
                <Line yAxisId="line" type="monotone" dataKey="Cumulative"
                      stroke="#fbbf24" strokeWidth={1.5} strokeOpacity={0.6}
                      dot={false} isAnimationActive={false} />
              </ComposedChart>
            ) : (
              <BarChart data={chartData as any} margin={{ top: 10, right: 10, left: 0, bottom: 0 }}>
                <XAxis dataKey="date" tick={{ fill: '#888', fontSize: 11 }} />
                <YAxis tick={{ fill: '#888', fontSize: 11 }} tickFormatter={fmtUsd} />
                <Tooltip contentStyle={{ background: '#0a0a0a', border: '1px solid rgba(255,255,255,0.15)', fontSize: 11 }} formatter={(v: any) => fmtUsd(Number(v))} />
                <Bar dataKey="TVL" fill="#a78bfa" fillOpacity={0.85} />
              </BarChart>
            )}
          </ResponsiveContainer>
          )}
        </div>
      </div>

      {/* Holders tabs */}
      <div className="mt-8 flex items-center gap-2 mb-3 flex-wrap">
        {(['PT', 'YT', 'LP'] as Leg[]).map(l => {
          const s = l === 'PT' ? ptSnap : l === 'YT' ? ytSnap : lpSnap;
          return (
            <button key={l} onClick={() => setTab(l)}
              className={`text-sm px-4 py-2 rounded-lg border ${tab === l ? 'border-white/30 bg-white/10 text-white' : 'border-white/10 text-white/40 hover:text-white'}`}>
              {l} Holders
              {s && <span className="text-white/30 ml-2">{s.holders} · {fmtUsd(s.totalUsd)}</span>}
            </button>
          );
        })}
      </div>

      <div className="mb-3">
        <input value={search} onChange={e => setSearch(e.target.value)}
          placeholder="Search wallet address…"
          className="w-full max-w-md bg-[#0a0a0a]/80 border border-white/10 focus:border-white/30 focus:outline-none rounded-md px-3 py-2 text-sm placeholder-white/20 font-mono" />
      </div>

      {/* Holders table */}
      <div className="rounded-2xl border border-white/10 bg-white/5 overflow-x-auto">
        <table className="w-full text-sm">
          <thead className="text-xs uppercase tracking-wider text-white/40">
            <tr>
              <th className="px-4 py-2 text-left">#</th>
              <th className="px-4 py-2 text-left">Wallet</th>
              <th className="px-4 py-2 text-right">Balance</th>
              <th className="px-4 py-2 text-right">Value</th>
              <th className="px-4 py-2 text-right">Share</th>
            </tr>
          </thead>
          <tbody className="divide-y divide-white/5 text-[13px]">
            {!cur?.top?.length && (
              <tr><td className="px-4 py-3 text-white/30" colSpan={5}>No holders for {tab}.</td></tr>
            )}
            {cur?.top?.filter(h => !search || h.owner.toLowerCase().includes(search.toLowerCase())).map((h, i) => (
              <tr key={h.owner} className="hover:bg-white/5 cursor-pointer"
                  onClick={() => window.location.href = `/wallet/?addr=${h.owner}`}>
                <td className="px-4 py-1.5 text-white/30 tabular-nums">{i + 1}</td>
                <td className="px-4 py-1.5">
                  <div className="flex items-center gap-2">
                    <span className="text-xs text-white/70 font-mono">{h.owner}</span>
                    <a onClick={e => e.stopPropagation()} className="text-white/20 hover:text-white/60 text-xs"
                       href={`https://solscan.io/account/${h.owner}`} target="_blank" rel="noopener noreferrer">↗</a>
                  </div>
                </td>
                <td className="px-4 py-1.5 text-right tabular-nums text-white">
                  {h.balance > 1e6 ? `${(h.balance/1e6).toFixed(2)}M` : h.balance > 1e3 ? `${(h.balance/1e3).toFixed(1)}K` : h.balance.toFixed(2)}
                </td>
                <td className="px-4 py-1.5 text-right tabular-nums text-emerald-400/80">
                  {h.usd > 0 ? fmtUsd(h.usd) : '–'}
                </td>
                <td className="px-4 py-1.5 text-right tabular-nums text-white/50">
                  <div className="flex items-center justify-end gap-2">
                    <div className="w-16 h-1.5 bg-white/10 rounded-full overflow-hidden">
                      <div className="h-full bg-emerald-400 rounded-full" style={{ width: `${Math.min(h.sharePct, 100)}%` }} />
                    </div>
                    <span>{h.sharePct.toFixed(1)}%</span>
                  </div>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      {cur && cur.holders > 500 && (
        <div className="text-xs text-white/30 mt-2 text-center">Showing top 500 of {cur.holders} holders</div>
      )}
    </main>
  );
}

function Stat({ label, value }: { label: string; value: string }) {
  return (
    <div className="rounded-xl border border-white/10 bg-white/5 px-3 py-2">
      <div className="text-[10px] uppercase tracking-wider text-white/40">{label}</div>
      <div className="mt-0.5 text-base font-semibold text-white tabular-nums">{value}</div>
    </div>
  );
}

function shortAddr(a: string) { return `${a.slice(0, 4)}…${a.slice(-4)}`; }

function fmtSy(n: number) {
  if (n >= 1e9) return `${(n / 1e9).toFixed(2)}B`;
  if (n >= 1e6) return `${(n / 1e6).toFixed(2)}M`;
  if (n >= 1e3) return `${(n / 1e3).toFixed(2)}K`;
  return n.toFixed(2);
}

type OrderbookSubview = 'ladder' | 'wallets';

function OrderbookView({
  market, snapshotDate, activeBookIdx, setActiveBookIdx,
}: {
  market: V2MarketBook;
  snapshotDate: string | null;
  activeBookIdx: number;
  setActiveBookIdx: (i: number) => void;
}) {
  const [subview, setSubview] = useState<OrderbookSubview>('ladder');
  const bookIdx = Math.min(activeBookIdx, market.books.length - 1);
  const book: V2Book | undefined = market.books[bookIdx];
  if (!book) return <div className="text-white/40 p-4">No book data.</div>;

  const bidTotal = book.bids.reduce((s, b) => s + b.sizeSy, 0);
  const askTotal = book.asks.reduce((s, b) => s + b.sizeSy, 0);
  const bestBid = book.bestBidApy;
  const bestAsk = book.bestAskApy;
  const spreadBps = (bestBid !== null && bestAsk !== null) ? (bestAsk - bestBid) * 10000 : null;

  // Per-wallet aggregation across both sides
  type WalletAgg = {
    wallet: string; nBids: number; nAsks: number; bidSy: number; askSy: number;
    bestBidApy: number | null; bestAskApy: number | null;
  };
  const wallets = new Map<string, WalletAgg>();
  for (const o of book.bids) {
    const w = wallets.get(o.owner) ?? { wallet: o.owner, nBids: 0, nAsks: 0, bidSy: 0, askSy: 0, bestBidApy: null, bestAskApy: null };
    w.nBids += 1; w.bidSy += o.sizeSy;
    if (w.bestBidApy === null || o.apy > w.bestBidApy) w.bestBidApy = o.apy;
    wallets.set(o.owner, w);
  }
  for (const o of book.asks) {
    const w = wallets.get(o.owner) ?? { wallet: o.owner, nBids: 0, nAsks: 0, bidSy: 0, askSy: 0, bestBidApy: null, bestAskApy: null };
    w.nAsks += 1; w.askSy += o.sizeSy;
    if (w.bestAskApy === null || o.apy < w.bestAskApy) w.bestAskApy = o.apy;
    wallets.set(o.owner, w);
  }
  const walletRows = Array.from(wallets.values()).sort((a, b) => (b.bidSy + b.askSy) - (a.bidSy + a.askSy));
  const nBidOnly = walletRows.filter(w => w.nBids > 0 && w.nAsks === 0).length;
  const nAskOnly = walletRows.filter(w => w.nBids === 0 && w.nAsks > 0).length;
  const nBoth    = walletRows.filter(w => w.nBids > 0 && w.nAsks > 0).length;

  // Build cumulative + pct-of-side for ladder bars
  let cumB = 0;
  const bidsView = book.bids.map(r => { cumB += r.sizeSy; return { ...r, cum: cumB, pct: bidTotal ? r.sizeSy / bidTotal : 0 }; });
  let cumA = 0;
  const asksView = book.asks.map(r => { cumA += r.sizeSy; return { ...r, cum: cumA, pct: askTotal ? r.sizeSy / askTotal : 0 }; });

  const fmtApy = (n: number | null) => n === null ? '–' : `${(n * 100).toFixed(2)}%`;
  const fmtSpread = (n: number | null) => n === null ? '–' : (n < 0 ? `crossed` : (n >= 1000 ? `${(n / 100).toFixed(2)}%` : `${n.toFixed(0)} bps`));

  return (
    <div>
      {/* Header — book selector (multi-book) + subview toggle + snapshot info */}
      <div className="flex items-center justify-between gap-2 mb-3 flex-wrap">
        <div className="flex items-center gap-2 flex-wrap">
          {market.books.length > 1 && market.books.map((b, i) => (
            <button key={b.bookAccount} onClick={() => setActiveBookIdx(i)}
              className={`text-[11px] px-2.5 py-1 rounded-md border ${bookIdx === i ? 'border-white/30 bg-white/10 text-white' : 'border-white/10 text-white/40 hover:text-white/70'}`}
              title={b.bookAccount}>
              Book {i + 1} <span className="text-white/40 ml-1">{shortAddr(b.bookAccount)}</span>
            </button>
          ))}
          {(['ladder','wallets'] as OrderbookSubview[]).map(v => (
            <button key={v} onClick={() => setSubview(v)}
              className={`text-[11px] px-2.5 py-1 rounded-md border ${subview === v ? 'border-white/30 bg-white/10 text-white' : 'border-white/10 text-white/40 hover:text-white/70'}`}>
              {v === 'ladder' ? 'Ladder' : `Wallets · ${walletRows.length}`}
            </button>
          ))}
        </div>
        <div className="text-[11px] text-white/40">
          snapshot {snapshotDate ?? '—'} · book <span className="font-mono">{shortAddr(book.bookAccount)}</span>
        </div>
      </div>

      {/* Stat strip */}
      <div className="grid grid-cols-2 md:grid-cols-3 lg:grid-cols-6 gap-2 mb-4">
        <Stat label="Bid top" value={fmtApy(bestBid)} />
        <Stat label="Ask top" value={fmtApy(bestAsk)} />
        <Stat label="Spread"  value={fmtSpread(spreadBps)} />
        <Stat label="Bids Σ" value={`${fmtSy(bidTotal)} SY`} />
        <Stat label="Asks Σ" value={`${fmtSy(askTotal)} SY`} />
        <Stat label="Orders" value={`${book.nBids} / ${book.nAsks}`} />
      </div>

      {subview === 'ladder' ? (
        <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
          <LadderTable side="bid"  rows={bidsView} totalSy={bidTotal} />
          <LadderTable side="ask"  rows={asksView} totalSy={askTotal} />
        </div>
      ) : (
        <WalletsTable rows={walletRows} nBidOnly={nBidOnly} nAskOnly={nAskOnly} nBoth={nBoth} />
      )}
    </div>
  );
}

function WalletsTable({
  rows, nBidOnly, nAskOnly, nBoth,
}: {
  rows: Array<{
    wallet: string; nBids: number; nAsks: number; bidSy: number; askSy: number;
    bestBidApy: number | null; bestAskApy: number | null;
  }>;
  nBidOnly: number; nAskOnly: number; nBoth: number;
}) {
  return (
    <div className="rounded-lg border border-white/5 overflow-hidden">
      <div className="px-3 py-2 text-xs text-white/50 border-b border-white/5 flex items-center justify-between flex-wrap gap-2">
        <span>{rows.length} unique wallets in book</span>
        <span className="text-[10px] text-white/40">
          <span className="text-emerald-300/80">{nBidOnly} bid-only</span>
          {' · '}
          <span className="text-rose-300/80">{nAskOnly} ask-only</span>
          {' · '}
          <span className="text-amber-300/80">{nBoth} both sides</span>
        </span>
      </div>
      <div className="overflow-x-auto">
        <table className="w-full text-xs">
          <thead className="text-white/40 text-[10px] uppercase tracking-wider">
            <tr className="border-b border-white/5">
              <th className="text-left  py-1.5 px-3 font-normal">Wallet</th>
              <th className="text-center py-1.5 px-2 font-normal">Side</th>
              <th className="text-right py-1.5 px-2 font-normal">Bids</th>
              <th className="text-right py-1.5 px-2 font-normal">Bid Σ</th>
              <th className="text-right py-1.5 px-2 font-normal">Best bid</th>
              <th className="text-right py-1.5 px-2 font-normal">Asks</th>
              <th className="text-right py-1.5 px-2 font-normal">Ask Σ</th>
              <th className="text-right py-1.5 px-2 font-normal">Best ask</th>
              <th className="text-right py-1.5 px-2 font-normal">Total SY</th>
            </tr>
          </thead>
          <tbody>
            {rows.map(w => {
              const both = w.nBids > 0 && w.nAsks > 0;
              const side = both ? 'B+A' : w.nBids > 0 ? 'B' : 'A';
              const sideColor = both ? 'text-amber-300' : w.nBids > 0 ? 'text-emerald-300' : 'text-rose-300';
              return (
                <tr key={w.wallet} className="border-b border-white/5 hover:bg-white/5 cursor-pointer"
                    onClick={() => window.location.href = `/wallet/?addr=${w.wallet}`}>
                  <td className="py-1 px-3 font-mono text-[11px] text-white/70 whitespace-nowrap">{w.wallet}</td>
                  <td className={`py-1 px-2 text-center text-[11px] font-medium ${sideColor}`}>{side}</td>
                  <td className="py-1 px-2 text-right tabular-nums text-white/60">{w.nBids || '–'}</td>
                  <td className="py-1 px-2 text-right tabular-nums text-emerald-200/80">{w.bidSy > 0 ? fmtSy(w.bidSy) : '–'}</td>
                  <td className="py-1 px-2 text-right tabular-nums text-emerald-300/70">{w.bestBidApy !== null ? `${(w.bestBidApy * 100).toFixed(2)}%` : '–'}</td>
                  <td className="py-1 px-2 text-right tabular-nums text-white/60">{w.nAsks || '–'}</td>
                  <td className="py-1 px-2 text-right tabular-nums text-rose-200/80">{w.askSy > 0 ? fmtSy(w.askSy) : '–'}</td>
                  <td className="py-1 px-2 text-right tabular-nums text-rose-300/70">{w.bestAskApy !== null ? `${(w.bestAskApy * 100).toFixed(2)}%` : '–'}</td>
                  <td className="py-1 px-2 text-right tabular-nums text-white/85 font-medium">{fmtSy(w.bidSy + w.askSy)}</td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>
    </div>
  );
}

function LadderTable({
  side, rows, totalSy,
}: {
  side: 'bid' | 'ask';
  rows: (V2OrderRow & { cum: number; pct: number })[];
  totalSy: number;
}) {
  const accent = side === 'bid' ? 'text-emerald-300' : 'text-rose-300';
  const barBg = side === 'bid' ? 'bg-emerald-400/15' : 'bg-rose-400/15';
  const label = side === 'bid' ? 'BIDS (Buy YT)' : 'ASKS (Sell YT)';
  if (rows.length === 0) {
    return (
      <div className="rounded-lg border border-white/5 p-4 text-white/30 text-xs">
        <div className="mb-2 text-white/40 uppercase tracking-wider text-[10px]">{label}</div>
        no resting orders
      </div>
    );
  }
  return (
    <div className="rounded-lg border border-white/5 overflow-hidden">
      <div className={`px-3 py-1.5 text-[10px] uppercase tracking-wider ${accent} border-b border-white/5`}>
        {label} · {rows.length} orders · Σ {fmtSy(totalSy)} SY
      </div>
      <table className="w-full text-xs">
        <thead className="text-white/40 text-[10px] uppercase tracking-wider">
          <tr className="border-b border-white/5">
            <th className="text-right py-1.5 px-2 font-normal">APY</th>
            <th className="text-right py-1.5 px-2 font-normal">Size</th>
            <th className="text-right py-1.5 px-2 font-normal">Cum</th>
            <th className="text-left  py-1.5 px-2 font-normal">Maker</th>
          </tr>
        </thead>
        <tbody>
          {rows.slice(0, 30).map((r, i) => (
            <tr key={`${r.owner}-${i}`} className="border-b border-white/5 relative">
              <td className={`relative py-1 px-2 text-right tabular-nums ${accent} font-medium`}>
                <div className={`absolute inset-y-0 right-0 ${barBg}`} style={{ width: `${Math.min(r.pct * 100 * 3, 100)}%` }} />
                <span className="relative">{(r.apy * 100).toFixed(2)}%</span>
              </td>
              <td className="py-1 px-2 text-right tabular-nums text-white/75">{fmtSy(r.sizeSy)}</td>
              <td className="py-1 px-2 text-right tabular-nums text-white/45">{fmtSy(r.cum)}</td>
              <td className="py-1 px-2 text-left font-mono text-[11px] text-white/55">{shortAddr(r.owner)}</td>
            </tr>
          ))}
        </tbody>
      </table>
      {rows.length > 30 && (
        <div className="text-[10px] text-white/30 text-center py-1.5 border-t border-white/5">
          top 30 of {rows.length}
        </div>
      )}
    </div>
  );
}

export default function MarketPage() {
  return (
    <Suspense fallback={<div className="mx-auto max-w-[1400px] px-4 py-10 text-white/50">Loading…</div>}>
      <MarketDetailView />
    </Suspense>
  );
}
