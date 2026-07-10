'use client';
import { Fragment, Suspense, useEffect, useMemo, useState } from 'react';
import Link from 'next/link';
import { useSearchParams } from 'next/navigation';
import {
  AreaChart, Area, BarChart, Bar, ComposedChart, LineChart, Line, XAxis, YAxis, Tooltip, ResponsiveContainer, ReferenceLine,
} from 'recharts';
import type { V2OrderbookData, V2MarketBook, V2Book, V2OrderRow } from '@/lib/types';

type Holder = { owner: string; balance: number; usd: number; sharePct: number;
  entryIy?: number | null; entryBuys?: number };
type Snapshot = {
  market: string; leg: string; holders: number;
  totalBalance: number; totalUsd: number; top: Holder[];
  avgEntryIy?: number | null; pricedHolders?: number;
};
type MarketHoldersData = {
  meta: any;
  byMarketLeg: Record<string, Snapshot>;
  historyByMarket?: Record<string, { dates: string[]; PT: number[]; YT: number[]; LP: number[] }>;
};

type TvlByMarket = Record<string, {
  ticker: string; tvlUsd: number[]; tvlUnderlying: number[]; principalUsd?: number[];
  ptUsd?: number[]; ytUsd?: number[]; lpUsd?: number[]; idleUsd?: number[];
  // Canonical (SDK time-curve) — for latest-day hero/position values.
  // ptUsd/ytUsd arrays use a noisier AMM-median, fine for chart history
  // but wrong for the headline number on low-volume markets.
  ptRatio?: number | null; impliedYield?: number | null;
  // Past-snapshot implied APY for the 7d delta. Sparse during early daily
  // history — `…Date` carries the actual lookback so we can label honestly.
  impliedYield7dAgo?: number | null; impliedYield7dAgoDate?: string | null;
  // Daily traded implied-yield series (aligned to dates; null on no-trade days).
  impliedYieldSeries?: (number | null)[];
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
type Metric = 'tvl' | 'iy' | 'breakdown' | 'positions' | 'volume' | 'holders' | 'orderbook';
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

// Parse "10SEP26" (the trailing maturity token in marketKey) into a Date.
const MONTH_ABBR: Record<string, number> = {
  JAN:0,FEB:1,MAR:2,APR:3,MAY:4,JUN:5,JUL:6,AUG:7,SEP:8,OCT:9,NOV:10,DEC:11,
};
function parseMaturity(key: string): Date | null {
  const tail = key.split('-').pop() || '';
  const m = tail.match(/^(\d{1,2})([A-Z]{3})(\d{2})$/);
  if (!m) return null;
  const mon = MONTH_ABBR[m[2]];
  if (mon === undefined) return null;
  return new Date(Date.UTC(2000 + parseInt(m[3]), mon, parseInt(m[1])));
}
function daysToMaturity(d: Date | null): number | null {
  if (!d) return null;
  return Math.ceil((d.getTime() - Date.now()) / 86400_000);
}
function prettyDate(d: Date | null): string {
  if (!d) return '—';
  return d.toLocaleDateString('en-US', { month: 'long', day: 'numeric', year: 'numeric', timeZone: 'UTC' });
}
// nDelta(series, n) = (last - last-n) / last-n. Returns null if not enough data.
function nDelta(series: number[], n = 7): number | null {
  if (series.length < n + 1) return null;
  const cur = series[series.length - 1], past = series[series.length - 1 - n];
  if (!past) return null;
  return (cur - past) / past;
}
// Fallback annualized APY from ptRatio. Used only when marketTvl.impliedYield
// is missing (older JSON builds). APY = (1/pt_ratio)^(1/years) − 1, matching
// Exponent's "Implied APY" convention.
function impliedYieldFromRatio(ptRatio: number | null | undefined, daysToMat: number | null): number | null {
  if (!ptRatio || !daysToMat || daysToMat <= 0) return null;
  if (ptRatio <= 0 || ptRatio >= 1) return null;
  return Math.pow(1 / ptRatio, 365 / daysToMat) - 1;
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
  const [range, setRange] = useState<Range>('30d');
  // Volume tab — toggle bars between absolute USD and per-day share %.
  const [volumeShare, setVolumeShare] = useState<boolean>(false);
  // Implied Yield tab — overlay a bid/ask order-book depth profile on the IY axis.
  const [showDepth, setShowDepth] = useState<boolean>(true);
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

  const tvlSeries = tvl?.byMarket?.[marketKey]?.tvlUsd ?? [];
  const tickerData = positions?.byTicker?.[ticker];
  const chartData = useMemo(() => {
    if (!tvl) return [];
    const start = rangeStart(tvl.dates, range);
    const dates = tvl.dates.slice(start);
    if (metric === 'tvl') {
      return dates.map((d, i) => ({ date: d, TVL: tvlSeries[start + i] || 0 }));
    }
    if (metric === 'iy') {
      const s = tvl.byMarket?.[marketKey]?.impliedYieldSeries ?? [];
      // Keep nulls so the line breaks on no-trade days instead of dropping to 0.
      const rows = dates.map((d, i) => ({ date: d, IY: s[start + i] ?? null }));
      // IY history is sparser than TVL history — trim leading/trailing empty
      // dates so the line fills the chart instead of hugging one edge.
      let a = 0, b = rows.length - 1;
      while (a <= b && rows[a].IY == null) a++;
      while (b >= a && rows[b].IY == null) b--;
      return a <= b ? rows.slice(a, b + 1) : rows;
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

  // Implied-yield chart y-domain + order-book depth profile (feature ②).
  // Bins the current bid/ask offers by their implied yield (apy) so the
  // right-side histogram lines up with the IY axis of the line chart —
  // showing where limit-order size clusters on the yield curve.
  const iyView = useMemo(() => {
    if (metric !== 'iy') return null;
    const s = (tvl?.byMarket?.[marketKey]?.impliedYieldSeries ?? [])
      .filter((v): v is number => v != null && isFinite(v));
    // Aggregate the ladder across ALL books of the market (BulkSOL runs two).
    const allBooks = v2Market?.books ?? [];
    const bids = allBooks.flatMap(b => b.bids ?? []).filter(o => isFinite(o.apy) && o.sizeSy > 0);
    const asks = allBooks.flatMap(b => b.asks ?? []).filter(o => isFinite(o.apy) && o.sizeSy > 0);

    // Anchor the yield axis on where the action is, not on dust limit orders
    // (far-out offers can sit at absurd APYs and would blow up the scale).
    // Center = traded IY range if we have it, else the bid/ask mid; keep
    // orders within ±20 percentage points of that center.
    const anchor = s.length
      ? (Math.min(...s) + Math.max(...s)) / 2
      : (v2Market?.midApy ?? 0.05);
    const near = (o: { apy: number }) => Math.abs(o.apy - anchor) <= 0.20;
    const nBids = bids.filter(near), nAsks = asks.filter(near);
    const vals = [...s, ...nBids.map(o => o.apy), ...nAsks.map(o => o.apy)];
    // Keep the current best bid/ask in view so the reference lines land inside.
    if (v2Market?.bestBidApy != null && Math.abs(v2Market.bestBidApy - anchor) <= 0.20) vals.push(v2Market.bestBidApy);
    if (v2Market?.bestAskApy != null && Math.abs(v2Market.bestAskApy - anchor) <= 0.20) vals.push(v2Market.bestAskApy);
    if (!vals.length) return { domain: [anchor - 0.05, anchor + 0.05] as [number, number], bins: [], maxSize: 0 };
    let lo = Math.min(...vals), hi = Math.max(...vals);
    const pad = (hi - lo) * 0.15 || 0.01;
    lo -= pad; hi += pad;

    // Fine-grained bins: ~10bps of implied yield each, clamped to a sane
    // count so bars stay ≥2px on the 220px plot.
    const N = Math.max(24, Math.min(80, Math.round((hi - lo) / 0.001)));
    const bins = Array.from({ length: N }, (_, i) => ({
      iy: lo + ((i + 0.5) * (hi - lo)) / N,
      bid: 0, ask: 0, bidN: 0, askN: 0,
    }));
    const put = (o: { apy: number; sizeSy: number }, key: 'bid' | 'ask') => {
      if (o.apy < lo || o.apy > hi) return;      // drop out-of-view dust
      const idx = Math.min(N - 1, Math.max(0, Math.floor(((o.apy - lo) / (hi - lo)) * N)));
      bins[idx][key] += o.sizeSy;
    };
    nBids.forEach(o => put(o, 'bid'));
    nAsks.forEach(o => put(o, 'ask'));
    // Ask size can dwarf bid size (5M vs 79K here), so normalise EACH side to
    // its own max — the profile shows where orders cluster on the yield curve
    // (shape), not bid-vs-ask absolute magnitude. Raw sizes stay for tooltips.
    const bidMax = Math.max(1e-9, ...bins.map(b => b.bid));
    const askMax = Math.max(1e-9, ...bins.map(b => b.ask));
    bins.forEach(b => { b.bidN = b.bid / bidMax; b.askN = b.ask / askMax; });
    const hasOrders = bidMax > 1e-9 || askMax > 1e-9;
    return { domain: [lo, hi] as [number, number], bins, hasOrders };
  }, [metric, tvl, marketKey, v2Market, activeBookIdx]);

  if (err) return <div className="mx-auto max-w-[1400px] px-4 py-10 text-red-400">Error: {err}</div>;
  if (!holders || !tvl || !positions) return <div className="mx-auto max-w-[1400px] px-4 py-10 text-white/50">Loading…</div>;

  const ptSnap = holders.byMarketLeg[`${marketKey}:PT`];
  const ytSnap = holders.byMarketLeg[`${marketKey}:YT`];
  const lpSnap = holders.byMarketLeg[`${marketKey}:LP`];
  const cur = tab === 'PT' ? ptSnap : tab === 'YT' ? ytSnap : lpSnap;

  const latestSupply = tickerData?.legs[tab]?.byMarket?.[marketKey] ?? [];
  const supply = latestSupply.length ? latestSupply[latestSupply.length - 1] : 0;
  const currentTvl = tvlSeries.length ? tvlSeries[tvlSeries.length - 1] : 0;

  // Derive headline numbers from the same JSON the existing components use.
  // No new data dependencies — just better story.
  const maturityDate = parseMaturity(marketKey);
  const daysToMat = daysToMaturity(maturityDate);
  const isExpired = daysToMat !== null && daysToMat <= 0;
  const marketTvl = tvl?.byMarket?.[marketKey];
  const lastN = (arr?: number[]) => (arr && arr.length ? arr[arr.length - 1] : 0);
  // Canonical SDK-curve ratio for the latest day. The historical ptUsd/ytUsd
  // arrays use a noisy AMM-median (fine for the chart, wrong for the headline
  // number on low-volume markets — e.g. ONyc-10SEP26 read 51% instead of 12.7%).
  // For the hero number and position cards, derive PT$ / YT$ from principalUsd[-1]
  // × ptRatio rather than reading the corrupted aggregates.
  const ptRatio = marketTvl?.ptRatio ?? null;
  const principalUsd = lastN(marketTvl?.principalUsd);
  const ptUsd = ptRatio !== null && ptRatio > 0 ? principalUsd * ptRatio : lastN(marketTvl?.ptUsd);
  const ytUsd = ptRatio !== null && ptRatio > 0 ? principalUsd * (1 - ptRatio) : lastN(marketTvl?.ytUsd);
  const lpUsd = lastN(marketTvl?.lpUsd);
  const ptSupply = lastN(tickerData?.legs.PT?.byMarket?.[marketKey]);
  const ytSupply = lastN(tickerData?.legs.YT?.byMarket?.[marketKey]);
  const lpSupply = lastN(tickerData?.legs.LP?.byMarket?.[marketKey]);
  const tvlDelta7d = nDelta(tvlSeries, 7);
  // Prefer the SDK-curve implied yield emitted by build_web_data.py. Fall back
  // to the closed-form -ln(ptRatio)/t if the JSON didn't ship that field.
  // Clamp to null when the implied-yield concept doesn't apply:
  //   - Expired markets: the on-chain rate is stale (the AMM still holds the
  //     pre-maturity ln_implied), so showing it as "Implied PT Yield" misleads.
  //   - v2-only markets (no PT supply): there's no PT to imply a yield from.
  const impliedRaw = marketTvl?.impliedYield ?? impliedYieldFromRatio(ptRatio, daysToMat);
  const implied = (isExpired || principalUsd <= 0) ? null : impliedRaw;
  // 7d delta for the implied yield hero. JSON ships the past value + actual
  // snapshot date (history is sparse during early Railway runs; we picked the
  // snapshot closest to 7 days). Render delta as a percentage-point change and
  // label with the actual lookback so the user sees what window we used.
  const impliedPast = marketTvl?.impliedYield7dAgo ?? null;
  const impliedPastDate = marketTvl?.impliedYield7dAgoDate ?? null;
  const impliedDeltaPct = impliedPast !== null && implied !== null && impliedPast > 0
    ? (implied - impliedPast) / impliedPast
    : null;
  const impliedLookbackDays = impliedPastDate
    ? Math.round((Date.now() - new Date(impliedPastDate).getTime()) / 86400_000)
    : null;
  // Total holders timeseries from historyByMarket → 7d relative delta.
  const holdersHist = holders?.historyByMarket?.[marketKey];
  const holdersTotals = holdersHist
    ? holdersHist.PT.map((v, i) => v + (holdersHist.YT[i] || 0) + (holdersHist.LP[i] || 0))
    : [];
  const holdersDelta7d = nDelta(holdersTotals, 7);
  const totalHolders =
    (ptSnap?.holders ?? 0) + (ytSnap?.holders ?? 0) + (lpSnap?.holders ?? 0);
  // platform name as a soft subtitle (falls back gracefully).
  const platform = marketTvl?.ticker ? '' : ''; // no platform string in tvl.json — keep empty

  // Tail sparkline window (last 30 points). Aligned with hero feel; not chart-replacement.
  const tvlSpark = tvlSeries.slice(-30);

  // Lifecycle progress — fraction of the market's life elapsed.
  // Pull start from the TVL series (first non-zero day) as a proxy
  // for "when this market opened." Capped at [0, 1].
  const firstActiveIdx = tvl?.dates ? (marketTvl?.tvlUsd ?? []).findIndex(v => v > 0) : -1;
  const startMs = firstActiveIdx >= 0 && tvl?.dates
    ? new Date(tvl.dates[firstActiveIdx]).getTime()
    : null;
  const lifeProgress = (startMs && maturityDate)
    ? Math.max(0, Math.min(1, (Date.now() - startMs) / (maturityDate.getTime() - startMs)))
    : null;

  return (
    <main className="mx-auto max-w-[1100px] px-4 sm:px-6 py-6">
      <Link href="/" className="text-[12px] text-white/30 hover:text-white/60">← Markets</Link>

      {/* ─── HEADER ─────────────────────────────────────────────── */}
      <header className="mt-4">
        <h1 className="text-[28px] font-medium tracking-tight text-white">
          {ticker} <span className="text-white/30">·</span>{' '}
          <span className="text-white/40 font-normal">{prettyDate(maturityDate)}</span>
        </h1>
        <div className="mt-1 text-[13px] text-white/40">
          {isExpired
            ? `Matured ${Math.abs(daysToMat ?? 0)} Days Ago`
            : `${daysToMat ?? '—'} Days to Maturity`}
        </div>
        {/* Quiet lifecycle bar — first/now/end implied by position */}
        {lifeProgress !== null && (
          <div className="mt-3 h-px bg-white/[0.07] relative overflow-visible">
            <div className="absolute inset-y-0 left-0 bg-white/40" style={{ width: `${lifeProgress * 100}%` }} />
            <div className="absolute -top-[3px] w-[7px] h-[7px] rounded-full bg-white" style={{ left: `calc(${lifeProgress * 100}% - 3.5px)` }} />
          </div>
        )}
      </header>

      {/* ─── HERO NUMBERS ─────────────────────────────────────── */}
      <section className="mt-8 grid grid-cols-1 sm:grid-cols-3 gap-x-12 gap-y-6">
        <HeroNumber
          value={fmtUsd(currentTvl)}
          label="Total Value Locked"
          sub={tvlDelta7d !== null
            ? `${tvlDelta7d > 0 ? '↑' : tvlDelta7d < 0 ? '↓' : '·'} ${Math.abs(tvlDelta7d * 100).toFixed(2)}% Past 7 Days`
            : undefined}
          subTone={tvlDelta7d === null ? 'mute' : tvlDelta7d > 0 ? 'up' : tvlDelta7d < 0 ? 'down' : 'mute'}
        />
        <HeroNumber
          value={implied === null ? '—' : `${(implied * 100).toFixed(2)}%`}
          label={isExpired ? 'Realized PT Yield' : 'Implied PT Yield'}
          sub={isExpired
            ? 'Settled at Par'
            : impliedDeltaPct !== null && impliedLookbackDays
              ? `${impliedDeltaPct > 0 ? '↑' : impliedDeltaPct < 0 ? '↓' : '·'} ${Math.abs(impliedDeltaPct * 100).toFixed(2)}% Past ${impliedLookbackDays} Days`
              : 'Fixed at Maturity'}
          subTone={isExpired || impliedDeltaPct === null
            ? 'mute'
            : impliedDeltaPct > 0 ? 'up'
            : impliedDeltaPct < 0 ? 'down'
            : 'mute'}
        />
        <HeroNumber
          value={totalHolders.toLocaleString()}
          label="Holders"
          sub={holdersDelta7d !== null
            ? `${holdersDelta7d > 0 ? '↑' : holdersDelta7d < 0 ? '↓' : '·'} ${Math.abs(holdersDelta7d * 100).toFixed(2)}% Past 7 Days`
            : 'PT + YT + LP'}
          subTone={holdersDelta7d === null ? 'mute' : holdersDelta7d > 0 ? 'up' : holdersDelta7d < 0 ? 'down' : 'mute'}
        />
      </section>

      {/* ─── POSITIONS ────────────────────────────────────────── */}
      <section className="mt-8 pt-5 border-t border-white/[0.06]">
        <h2 className="text-[11px] uppercase tracking-[0.18em] text-white/35 mb-4">Positions</h2>
        <div className="grid grid-cols-1 sm:grid-cols-3 gap-x-12 gap-y-6">
          <PositionLine label="Principal"  ticker={ticker} usd={ptUsd} supply={ptSupply} holders={ptSnap?.holders ?? null} />
          <PositionLine label="Yield"      ticker={ticker} usd={ytUsd} supply={ytSupply} holders={ytSnap?.holders ?? null} />
          <PositionLine label="Liquidity"  ticker={ticker} usd={lpUsd} supply={lpSupply} holders={lpSnap?.holders ?? null} />
        </div>
      </section>

      {/* ─── ACTIVITY ──────────────────────────────────────── */}
      <section className="mt-8 pt-5 border-t border-white/[0.06]">
        <h2 className="text-[11px] uppercase tracking-[0.18em] text-white/35 mb-4">Activity</h2>

        <div className="flex items-center justify-between mb-3 flex-wrap gap-3">
          <div className="flex items-center gap-5 flex-wrap text-[13px]">
            {(['tvl', 'iy', 'breakdown', 'positions', 'volume', 'holders', 'orderbook'] as Metric[]).map(m => {
              if (m === 'orderbook' && !v2Market) return null;
              const label = m === 'tvl' ? 'TVL'
                : m === 'iy' ? 'Implied Yield'
                : m === 'breakdown' ? 'Breakdown'
                : m === 'positions' ? 'Positions'
                : m === 'volume' ? 'Volume'
                : m === 'holders' ? 'Holders'
                : 'Orderbook';
              const isActive = metric === m;
              return (
                <button key={m} onClick={() => setMetric(m)}
                  className={`relative pb-2 transition ${isActive ? 'text-white' : 'text-white/35 hover:text-white/70'}`}>
                  {label}
                  {isActive && <span className="absolute left-0 right-0 -bottom-px h-px bg-white" />}
                </button>
              );
            })}
          </div>
          <div className="flex items-center gap-1 text-[12px]">
            {metric === 'volume' && (
              <button onClick={() => setVolumeShare(v => !v)}
                className={`px-2 py-1 mr-2 transition ${volumeShare ? 'text-white' : 'text-white/30 hover:text-white/60'}`}
                title="Toggle Share % stacked view">
                Share%
              </button>
            )}
            {metric === 'iy' && v2Market && (
              <button onClick={() => setShowDepth(v => !v)}
                className={`px-2 py-1 mr-2 transition ${showDepth ? 'text-white' : 'text-white/30 hover:text-white/60'}`}
                title="Overlay bid/ask limit-order depth on the yield axis">
                Depth
              </button>
            )}
            {(['30d', '90d', '1y', 'all'] as Range[]).map(r => (
              <button key={r} onClick={() => setRange(r)}
                className={`px-2 py-1 tabular-nums transition ${range === r ? 'text-white' : 'text-white/30 hover:text-white/60'}`}>
                {r === 'all' ? 'All' : r.toUpperCase()}
              </button>
            ))}
          </div>
        </div>
        <div className="pt-2">
          {metric === 'orderbook' && v2Market ? (
            <OrderbookView
              market={v2Market}
              activeBookIdx={activeBookIdx}
              setActiveBookIdx={setActiveBookIdx}
            />
          ) : metric === 'iy' && !(chartData as any[]).some((r: any) => r.IY != null) ? (
            <div className="h-[260px] flex items-center justify-center text-white/30 text-sm">
              No implied-yield history for this market yet — it accrues one point per daily refresh, plus trade-derived backfill.
            </div>
          ) : metric === 'iy' ? (
            <div className="flex gap-2" style={{ height: 260 }}>
              <div className="flex-1 min-w-0">
                <ResponsiveContainer width="100%" height={260}>
                  <LineChart data={chartData as any} margin={{ top: 10, right: 4, left: 0, bottom: 0 }}>
                    <XAxis dataKey="date" tick={{ fill: '#888', fontSize: 11 }} />
                    <YAxis domain={iyView ? iyView.domain : ['auto', 'auto']}
                           tick={{ fill: '#888', fontSize: 11 }}
                           tickFormatter={(v: number) => `${(v * 100).toFixed(1)}%`} width={52} />
                    <Tooltip contentStyle={{ background: '#0a0a0a', border: '1px solid rgba(255,255,255,0.15)', fontSize: 11 }}
                             formatter={(v: any) => v == null ? '—' : `${(Number(v) * 100).toFixed(2)}%`} />
                    {/* Live book context: current best bid / best ask levels */}
                    {showDepth && v2Market?.bestBidApy != null && (
                      <ReferenceLine y={v2Market.bestBidApy} stroke="#4ade80" strokeDasharray="4 3" strokeOpacity={0.5}
                        label={{ value: `bid ${(v2Market.bestBidApy * 100).toFixed(2)}%`, position: 'insideRight', fill: '#4ade80', fontSize: 10, opacity: 0.8 }} />
                    )}
                    {showDepth && v2Market?.bestAskApy != null && (
                      <ReferenceLine y={v2Market.bestAskApy} stroke="#f87171" strokeDasharray="4 3" strokeOpacity={0.5}
                        label={{ value: `ask ${(v2Market.bestAskApy * 100).toFixed(2)}%`, position: 'insideRight', fill: '#f87171', fontSize: 10, opacity: 0.8 }} />
                    )}
                    <Line type="monotone" dataKey="IY" stroke="#38bdf8" strokeWidth={1.6}
                          dot={false} connectNulls isAnimationActive={false} />
                  </LineChart>
                </ResponsiveContainer>
              </div>
              {showDepth && v2Market && iyView && iyView.hasOrders && (() => {
                // Custom div profile — recharts vertical bars are category-
                // positioned, not value-positioned, so they won't align to the
                // line's numeric axis. We map each bin's IY to the exact same
                // plot area (top 10px, height 220px = 260 − 30px x-axis) as the
                // LineChart so bars line up with the yield the line is at.
                const [lo, hi] = iyView.domain;
                const PLOT_TOP = 10, PLOT_H = 220;
                const N = iyView.bins.length;
                const binH = PLOT_H / N;
                const yOf = (iy: number) => PLOT_TOP + (1 - (iy - lo) / (hi - lo)) * PLOT_H;
                return (
                  <div style={{ width: 140 }} className="shrink-0 relative" title="Limit-order size by implied yield (each side scaled to its own max)">
                    <div className="absolute top-0 left-0 right-0 z-10 text-[9px] uppercase tracking-[0.14em] text-white/30 text-center flex justify-center gap-3">
                      <span><span className="text-emerald-400">■</span> bids</span>
                      <span><span className="text-rose-400">■</span> asks</span>
                    </div>
                    <div className="relative" style={{ height: 260 }}>
                      {iyView.bins.map((b, i) => {
                        const top = yOf(b.iy) - binH / 2;
                        return (
                          <Fragment key={i}>
                            {b.bid > 0 && (
                              <div className="absolute left-0 bg-emerald-400/80 rounded-sm"
                                   style={{ top, height: Math.max(1.5, binH - 1), width: `${Math.max(1.5, b.bidN * 100)}%` }}
                                   title={`${(b.iy * 100).toFixed(2)}% · ${fmtCount(b.bid, '').trim()} SY bids`} />
                            )}
                            {b.ask > 0 && (
                              <div className="absolute left-0 bg-rose-400/80 rounded-sm"
                                   style={{ top, height: Math.max(1.5, binH - 1), width: `${Math.max(1.5, b.askN * 100)}%` }}
                                   title={`${(b.iy * 100).toFixed(2)}% · ${fmtCount(b.ask, '').trim()} SY asks`} />
                            )}
                          </Fragment>
                        );
                      })}
                    </div>
                    {/* Book stats — mid / spread / resting size per side */}
                    <div className="text-[9px] text-white/35 text-center leading-relaxed -mt-4">
                      {v2Market.midApy != null && <>mid {(v2Market.midApy * 100).toFixed(2)}%</>}
                      {v2Market.spreadBps != null && <> · {Math.round(v2Market.spreadBps)}bps spread</>}
                      <br />
                      {fmtCount(v2Market.totalBidSizeSy ?? 0, '').trim()} bid · {fmtCount(v2Market.totalAskSizeSy ?? 0, '').trim()} ask SY
                    </div>
                  </div>
                );
              })()}
            </div>
          ) : (
          <ResponsiveContainer width="100%" height={260}>
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
      </section>

      {/* Holders tabs — only on the Holders metric */}
      {metric === 'holders' && (
      <>
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

      {cur && (cur.avgEntryIy != null || (cur.pricedHolders ?? 0) > 0) && (
        <div className="mb-3 text-[13px] text-white/60">
          Avg entry implied yield ({tab}):{' '}
          <span className="text-white tabular-nums">
            {cur.avgEntryIy != null ? `${(cur.avgEntryIy * 100).toFixed(2)}%` : '—'}
          </span>
          <span className="text-white/30"> · across {cur.pricedHolders ?? 0} traders</span>
          <span className="text-white/25 ml-2 text-[11px]">
            (holders who acquired on-market; minters excluded)
          </span>
        </div>
      )}

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
              <th className="px-4 py-2 text-right" title="Volume-weighted implied yield at which this holder bought on-market. — = acquired by minting/transfer, no market entry.">Entry IY</th>
              <th className="px-4 py-2 text-right">Share</th>
            </tr>
          </thead>
          <tbody className="divide-y divide-white/5 text-[13px]">
            {!cur?.top?.length && (
              <tr><td className="px-4 py-3 text-white/30" colSpan={6}>No holders for {tab}.</td></tr>
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
                <td className="px-4 py-1.5 text-right tabular-nums text-sky-300/80"
                    title={h.entryBuys ? `${h.entryBuys} buy${h.entryBuys > 1 ? 's' : ''}` : undefined}>
                  {h.entryIy != null ? `${(h.entryIy * 100).toFixed(2)}%` : <span className="text-white/20">—</span>}
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
      </>
      )}
    </main>
  );
}

function Stat({ label, value }: { label: string; value: string }) {
  return (
    <div className="border-l border-white/[0.08] pl-3 py-1">
      <div className="text-[10px] uppercase tracking-[0.16em] text-white/35">{label}</div>
      <div className="mt-0.5 text-[15px] text-white tabular-nums">{value}</div>
    </div>
  );
}

// One hero number with a small label underneath and an optional one-line subline.
// Hierarchy is size + weight + spacing only — no cards, no borders.
function HeroNumber({ value, label, sub, subTone }: {
  value: string; label: string; sub?: string;
  subTone?: 'up' | 'down' | 'mute';
}) {
  const subClass =
    subTone === 'up'   ? 'text-emerald-400/80' :
    subTone === 'down' ? 'text-rose-400/80'    :
                         'text-white/35';
  return (
    <div>
      <div className="text-[36px] leading-none font-medium tracking-tight text-white tabular-nums">{value}</div>
      <div className="mt-2 text-[13px] text-white/55">{label}</div>
      {sub && <div className={`mt-1 text-[12px] tabular-nums ${subClass}`}>{sub}</div>}
    </div>
  );
}

// One position column. Label / $value / single compact meta line.
// Supply + holders collapse into one inline KV with a hairline center-dot
// separator so the meta sits anchored under the $value rather than
// floating in a 2-col sub-grid with trailing whitespace.
function PositionLine({ label, ticker, usd, supply, holders }: {
  label: string; ticker: string;
  usd: number; supply: number; holders: number | null;
}) {
  const dash = '—';
  const supplyStr = supply > 0 ? `${fmtCount(supply, '').trim()} ${ticker}` : dash;
  const holdersStr = holders !== null ? `${holders.toLocaleString()} holders` : `${dash} holders`;
  return (
    <div>
      <div className="text-[11px] uppercase tracking-[0.18em] text-white/35">{label}</div>
      <div className="mt-2 text-[24px] leading-none font-medium tracking-tight text-white tabular-nums">
        {usd > 0 ? fmtUsd(usd) : dash}
      </div>
      <div className="mt-2 text-[12px] text-white/55">
        <span className="tabular-nums">{supplyStr}</span>
        <span className="mx-2 text-white/25">·</span>
        <span className="tabular-nums">{holdersStr}</span>
      </div>
    </div>
  );
}

function shortAddr(a: string) { return `${a.slice(0, 4)}…${a.slice(-4)}`; }

function CopyButton({ value }: { value: string }) {
  const [copied, setCopied] = useState(false);
  return (
    <button
      onClick={(e) => {
        e.stopPropagation();
        navigator.clipboard.writeText(value).then(() => {
          setCopied(true);
          setTimeout(() => setCopied(false), 1200);
        });
      }}
      title={copied ? 'Copied!' : 'Copy address'}
      className={`inline-flex items-center justify-center align-middle ml-1.5 w-4 h-4 rounded transition ${
        copied ? 'text-emerald-300' : 'text-white/25 hover:text-white/70 hover:bg-white/5'
      }`}
    >
      {copied ? (
        <svg width="11" height="11" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="3" strokeLinecap="round" strokeLinejoin="round">
          <polyline points="20 6 9 17 4 12" />
        </svg>
      ) : (
        <svg width="11" height="11" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
          <rect x="9" y="9" width="13" height="13" rx="2" ry="2" />
          <path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1" />
        </svg>
      )}
    </button>
  );
}

function fmtSy(n: number) {
  if (n >= 1e9) return `${(n / 1e9).toFixed(2)}B`;
  if (n >= 1e6) return `${(n / 1e6).toFixed(2)}M`;
  if (n >= 1e3) return `${(n / 1e3).toFixed(2)}K`;
  return n.toFixed(2);
}

type AggMode = 'aggregate' | 'expanded';

type Tick = {
  side: 'bid' | 'ask';
  apy: number;
  nOrders: number;
  nWallets: number;
  sizeSy: number;
  orders: V2OrderRow[];
};

function fmtCreatedAt(unixSec: number): string {
  const d = new Date(unixSec * 1000);
  const mm = String(d.getUTCMonth() + 1).padStart(2, '0');
  const dd = String(d.getUTCDate()).padStart(2, '0');
  const hh = String(d.getUTCHours()).padStart(2, '0');
  const mi = String(d.getUTCMinutes()).padStart(2, '0');
  return `${mm}-${dd} ${hh}:${mi} UTC`;
}

function aggregateTicks(rows: V2OrderRow[], side: 'bid' | 'ask'): Tick[] {
  const byApy = new Map<number, Tick>();
  for (const r of rows) {
    let t = byApy.get(r.apy);
    if (!t) {
      t = { side, apy: r.apy, nOrders: 0, nWallets: 0, sizeSy: 0, orders: [] };
      byApy.set(r.apy, t);
    }
    t.nOrders += 1;
    t.sizeSy += r.sizeSy;
    t.orders.push(r);
  }
  for (const t of byApy.values()) {
    t.nWallets = new Set(t.orders.map(o => o.owner)).size;
    // largest first within a tick (expanded view ordering)
    t.orders.sort((a, b) => b.sizeSy - a.sizeSy);
  }
  // both sides sorted by APY DESC per spec
  return Array.from(byApy.values()).sort((a, b) => b.apy - a.apy);
}

function OrderbookView({
  market, activeBookIdx, setActiveBookIdx,
}: {
  market: V2MarketBook;
  activeBookIdx: number;
  setActiveBookIdx: (i: number) => void;
}) {
  const [aggMode, setAggMode] = useState<AggMode>('aggregate');
  const [walletSearch, setWalletSearch] = useState('');
  const bookIdx = Math.min(activeBookIdx, market.books.length - 1);
  const book: V2Book | undefined = market.books[bookIdx];
  if (!book) return <div className="text-white/40 p-4">No book data.</div>;

  const bidTotal = book.bids.reduce((s, b) => s + b.sizeSy, 0);
  const askTotal = book.asks.reduce((s, b) => s + b.sizeSy, 0);
  const bestBid = book.bestBidApy;
  const bestAsk = book.bestAskApy;
  const spreadBps = (bestBid !== null && bestAsk !== null) ? (bestAsk - bestBid) * 10000 : null;

  const askTicks = aggregateTicks(book.asks, 'ask');
  const bidTicks = aggregateTicks(book.bids, 'bid');

  const fmtApy = (n: number | null) => n === null ? '–' : `${(n * 100).toFixed(2)}%`;
  const fmtSpread = (n: number | null) => n === null ? '–' : (n < 0 ? `crossed` : (n >= 1000 ? `${(n / 100).toFixed(2)}%` : `${n.toFixed(0)} bps`));

  return (
    <div>
      {/* Toolbar — view mode + book selector + search, single row,
          underline-tab language matching the Activity nav above. */}
      <div className="flex items-center justify-between mb-4 flex-wrap gap-3">
        <div className="flex items-center gap-5 flex-wrap text-[13px]">
          {(['aggregate','expanded'] as AggMode[]).map(v => {
            const isActive = aggMode === v;
            return (
              <button key={v} onClick={() => setAggMode(v)}
                className={`relative pb-2 transition ${isActive ? 'text-white' : 'text-white/35 hover:text-white/70'}`}>
                {v === 'aggregate' ? 'Aggregate' : 'Expanded'}
                {isActive && <span className="absolute left-0 right-0 -bottom-px h-px bg-white" />}
              </button>
            );
          })}
          {market.books.length > 1 && (
            <>
              <span className="h-3 w-px bg-white/10" />
              {market.books.map((b, i) => {
                const isActive = bookIdx === i;
                return (
                  <button key={b.bookAccount} onClick={() => setActiveBookIdx(i)}
                    className={`relative pb-2 transition ${isActive ? 'text-white' : 'text-white/35 hover:text-white/70'}`}
                    title={b.bookAccount}>
                    Book {i + 1}
                    {isActive && <span className="absolute left-0 right-0 -bottom-px h-px bg-white" />}
                  </button>
                );
              })}
            </>
          )}
        </div>
        <div className="relative">
          <svg className="absolute left-2 top-1/2 -translate-y-1/2 w-3 h-3 text-white/30 pointer-events-none"
               viewBox="0 0 24 24" fill="none" stroke="currentColor"
               strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
            <circle cx="11" cy="11" r="7" />
            <line x1="21" y1="21" x2="16.65" y2="16.65" />
          </svg>
          <input
            value={walletSearch}
            onChange={(e) => setWalletSearch(e.target.value)}
            placeholder="Search wallets"
            className="w-[200px] bg-transparent border-b border-white/[0.08] focus:border-white/30 focus:outline-none pl-6 pr-2 py-1 text-[12px] placeholder-white/25 font-mono transition"
          />
        </div>
      </div>

      {/* Stat strip */}
      <div className="grid grid-cols-2 md:grid-cols-3 lg:grid-cols-6 gap-2 mb-4">
        <Stat label="Bid Top" value={fmtApy(bestBid)} />
        <Stat label="Ask Top" value={fmtApy(bestAsk)} />
        <Stat label="Spread"  value={fmtSpread(spreadBps)} />
        <Stat label="Bids Σ" value={`${fmtSy(bidTotal)} SY`} />
        <Stat label="Asks Σ" value={`${fmtSy(askTotal)} SY`} />
        <Stat label="Orders" value={`${book.nBids} / ${book.nAsks}`} />
      </div>

      <UnifiedLadder
        askTicks={askTicks}
        bidTicks={bidTicks}
        bidTotal={bidTotal}
        askTotal={askTotal}
        spreadBps={spreadBps}
        aggMode={aggMode}
        bestBid={bestBid}
        bestAsk={bestAsk}
        walletSearch={walletSearch}
      />
    </div>
  );
}

function UnifiedLadder({
  askTicks, bidTicks, bidTotal, askTotal, spreadBps, aggMode, walletSearch,
}: {
  askTicks: Tick[];
  bidTicks: Tick[];
  bidTotal: number;
  askTotal: number;
  spreadBps: number | null;
  aggMode: AggMode;
  bestBid: number | null;
  bestAsk: number | null;
  walletSearch: string;
}) {
  const sizeCell = (sz: number, side: 'bid' | 'ask') => {
    const denom = side === 'bid' ? bidTotal : askTotal;
    const pct = denom ? sz / denom : 0;
    const bar = side === 'bid' ? 'bg-emerald-400/20' : 'bg-rose-400/20';
    return (
      <td className="relative py-1.5 px-3 text-right tabular-nums text-white/85">
        <div className={`absolute inset-y-0 right-0 ${bar}`} style={{ width: `${Math.min(pct * 100 * 2.5, 100)}%` }} />
        <span className="relative">{fmtSy(sz)}</span>
      </td>
    );
  };
  const q = walletSearch.trim().toLowerCase();
  const matchAddr = (a: string) => !q || a.toLowerCase().includes(q);
  const tickMatches = (t: Tick) => !q || t.orders.some(o => matchAddr(o.owner));

  const spreadStr =
    spreadBps === null ? '–'
    : spreadBps < 0 ? 'crossed'
    : spreadBps >= 1000 ? `${(spreadBps/100).toFixed(2)}%`
    : `${spreadBps.toFixed(0)} bps`;

  if (aggMode === 'aggregate') {
    const askFiltered = askTicks.filter(tickMatches);
    const bidFiltered = bidTicks.filter(tickMatches);
    return (
      <div className="overflow-x-auto">
        <table className="w-full text-xs">
          <thead className="text-white/40 border-b border-white/10">
            <tr>
              <th className="py-2 text-left  font-normal">Side</th>
              <th className="py-2 text-right font-normal">APY</th>
              <th className="py-2 text-right font-normal">Orders</th>
              <th className="py-2 text-right font-normal">Wallets</th>
              <th className="py-2 text-right font-normal">Size SY</th>
            </tr>
          </thead>
          <tbody>
            {askFiltered.map(t => (
              <tr key={`a-${t.apy}`} className="border-b border-white/5">
                <td className="py-1.5 text-rose-300/90">ASK</td>
                <td className="py-1.5 text-right tabular-nums text-white/85">{(t.apy * 100).toFixed(2)}%</td>
                <td className="py-1.5 text-right tabular-nums text-white/70">{t.nOrders}</td>
                <td className="py-1.5 text-right tabular-nums text-white/70">{t.nWallets}</td>
                {sizeCell(t.sizeSy, 'ask')}
              </tr>
            ))}
            {bidFiltered.length > 0 && askFiltered.length > 0 && (
              <tr className="border-b border-white/10">
                <td colSpan={5} className="py-2 text-center text-[11px] text-white/40 tabular-nums">spread {spreadStr}</td>
              </tr>
            )}
            {bidFiltered.map(t => (
              <tr key={`b-${t.apy}`} className="border-b border-white/5">
                <td className="py-1.5 text-emerald-300/90">BID</td>
                <td className="py-1.5 text-right tabular-nums text-white/85">{(t.apy * 100).toFixed(2)}%</td>
                <td className="py-1.5 text-right tabular-nums text-white/70">{t.nOrders}</td>
                <td className="py-1.5 text-right tabular-nums text-white/70">{t.nWallets}</td>
                {sizeCell(t.sizeSy, 'bid')}
              </tr>
            ))}
            {q && askFiltered.length + bidFiltered.length === 0 && (
              <tr><td colSpan={5} className="py-4 text-center text-white/40 text-xs">No wallets match &quot;{walletSearch}&quot;.</td></tr>
            )}
          </tbody>
        </table>
      </div>
    );
  }

  // Expanded: one row per order
  const askRows = askTicks.flatMap(t => t.orders.map(o => ({ ...o, side: 'ask' as const }))).filter(o => matchAddr(o.owner));
  const bidRows = bidTicks.flatMap(t => t.orders.map(o => ({ ...o, side: 'bid' as const }))).filter(o => matchAddr(o.owner));

  return (
    <div className="overflow-x-auto">
      <table className="w-full text-xs">
        <thead className="text-white/40 border-b border-white/10">
          <tr>
            <th className="py-2 text-left  font-normal">Side</th>
            <th className="py-2 text-right font-normal">APY</th>
            <th className="py-2 text-left  font-normal pl-4">Wallet</th>
            <th className="py-2 text-right font-normal">Size SY</th>
            <th className="py-2 text-right font-normal">Created</th>
          </tr>
        </thead>
        <tbody>
          {askRows.map((o, i) => (
            <tr key={`a-${o.apy}-${o.owner}-${i}`}
                className="border-b border-white/5 hover:bg-white/5 cursor-pointer"
                onClick={() => window.location.href = `/wallet/?addr=${o.owner}`}>
              <td className="py-1.5 text-rose-300/90">ASK</td>
              <td className="py-1.5 text-right tabular-nums text-white/85">{(o.apy * 100).toFixed(2)}%</td>
              <td className="py-1.5 text-left font-mono text-[11px] text-white/70 pl-4 whitespace-nowrap">
                {o.owner}<CopyButton value={o.owner} />
              </td>
              {sizeCell(o.sizeSy, 'ask')}
              <td className="py-1.5 text-right tabular-nums text-white/40">{fmtCreatedAt(o.createdAt)}</td>
            </tr>
          ))}
          {bidRows.length > 0 && askRows.length > 0 && (
            <tr className="border-b border-white/10">
              <td colSpan={5} className="py-2 text-center text-[11px] text-white/40 tabular-nums">spread {spreadStr}</td>
            </tr>
          )}
          {bidRows.map((o, i) => (
            <tr key={`b-${o.apy}-${o.owner}-${i}`}
                className="border-b border-white/5 hover:bg-white/5 cursor-pointer"
                onClick={() => window.location.href = `/wallet/?addr=${o.owner}`}>
              <td className="py-1.5 text-emerald-300/90">BID</td>
              <td className="py-1.5 text-right tabular-nums text-white/85">{(o.apy * 100).toFixed(2)}%</td>
              <td className="py-1.5 text-left font-mono text-[11px] text-white/70 pl-4 whitespace-nowrap">
                {o.owner}<CopyButton value={o.owner} />
              </td>
              {sizeCell(o.sizeSy, 'bid')}
              <td className="py-1.5 text-right tabular-nums text-white/40">{fmtCreatedAt(o.createdAt)}</td>
            </tr>
          ))}
          {q && askRows.length + bidRows.length === 0 && (
            <tr><td colSpan={5} className="py-4 text-center text-white/40 text-xs">No wallets match &quot;{walletSearch}&quot;.</td></tr>
          )}
        </tbody>
      </table>
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
