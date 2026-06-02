'use client';
import { useEffect, useMemo, useState } from 'react';
import type { V2OrderbookData } from '@/lib/types';

type ApLeg = { byMarket: Record<string, number[]>; totals: number[] };
type ApTicker = { underlyingMint: string; latest: Record<string, number>; legs: Record<string, ApLeg> };
type ApData = { byTicker: Record<string, ApTicker>; meta: { tickers: { ticker: string; marketCount: number }[] } };

type TvlByMarket = Record<string, {
  ticker: string; platform: string; tvlUsd: number[];
  ptUsd?: number[]; ytUsd?: number[]; lpUsd?: number[]; idleUsd?: number[];
  isTest?: boolean;
  liquidityUsd?: number;
}>;
type TvlData = { byMarket: TvlByMarket };

type Holders = {
  byMarketLeg: Record<string, { holders: number }>;
  holdersByMarket?: Record<string, number>;
};

type VolumeData = {
  dates: string[];
  byMarket?: Record<string, { totalUsd?: number[] }>;
};

type StatusFilter = 'active' | 'all' | 'v2';

function fmtUsd(n: number) {
  if (!n) return '–';
  if (n >= 1e9) return `$${(n / 1e9).toFixed(2)}B`;
  if (n >= 1e6) return `$${(n / 1e6).toFixed(2)}M`;
  if (n >= 1e3) return `$${(n / 1e3).toFixed(1)}K`;
  return `$${n.toFixed(2)}`;
}

function fmtSy(n: number | null) {
  if (!n) return '–';
  if (n >= 1e9) return `${(n / 1e9).toFixed(2)}B SY`;
  if (n >= 1e6) return `${(n / 1e6).toFixed(2)}M SY`;
  if (n >= 1e3) return `${(n / 1e3).toFixed(1)}K SY`;
  return `${n.toFixed(0)} SY`;
}

const MONTH_ABBR_TO_NUM: Record<string, number> = {
  JAN: 0, FEB: 1, MAR: 2, APR: 3, MAY: 4, JUN: 5,
  JUL: 6, AUG: 7, SEP: 8, OCT: 9, NOV: 10, DEC: 11,
};
function maturityMs(marketKey: string): number | null {
  const m = marketKey.match(/-(\d{2})([A-Z]{3})(\d{2})$/);
  if (!m) return null;
  const month = MONTH_ABBR_TO_NUM[m[2]];
  if (month === undefined) return null;
  return Date.UTC(2000 + Number(m[3]), month, Number(m[1]));
}

type SortKey =
  | 'marketKey' | 'platform' | 'tvlUsd' | 'ptUsd' | 'ytUsd' | 'lpUsd' | 'idleUsd'
  | 'liquidityUsd' | 'volume7dUsd' | 'holders'
  | 'oiSy';

export function MarketsList() {
  const [tvl, setTvl] = useState<TvlData | null>(null);
  const [ap, setAp] = useState<ApData | null>(null);
  const [holders, setHolders] = useState<Holders | null>(null);
  const [volume, setVolume] = useState<VolumeData | null>(null);
  const [v2, setV2] = useState<V2OrderbookData | null>(null);
  const [sortKey, setSortKey] = useState<SortKey>('tvlUsd');
  const [sortDesc, setSortDesc] = useState<boolean>(true);
  const [status, setStatus] = useState<StatusFilter>('active');

  useEffect(() => {
    fetch('/tvl.json').then(r => r.json()).then(setTvl).catch(() => null);
    fetch('/active_positions.json').then(r => r.json()).then(setAp).catch(() => null);
    fetch('/market_holders.json').then(r => r.json()).then(setHolders).catch(() => null);
    fetch('/volume.json').then(r => r.json()).then(setVolume).catch(() => null);
    fetch('/v2_orderbook.json').then(r => r.json()).then(setV2).catch(() => null);
  }, []);

  const rows = useMemo(() => {
    if (!tvl || !ap) return [];
    const now = new Date();
    const todayMs = Date.UTC(now.getUTCFullYear(), now.getUTCMonth(), now.getUTCDate());
    type Row = {
      marketKey: string; ticker: string; platform: string;
      tvlUsd: number; ptUsd: number; ytUsd: number; lpUsd: number; idleUsd: number;
      liquidityUsd: number; volume7dUsd: number; ptSupply: number; holders: number;
      isActive: boolean; isTest: boolean;
      // v2 fields
      hasV2: boolean; bookCount: number;
      oiSy: number;
    };
    const out: Row[] = [];
    const seenV2: Set<string> = new Set();
    for (const [mk, m] of Object.entries(tvl.byMarket)) {
      const ticker = m.ticker;
      const platform = m.platform || 'Other';
      const last = (arr?: number[]) => (arr && arr.length ? arr[arr.length - 1] || 0 : 0);
      const tvlUsd = last(m.tvlUsd);
      const ptUsd   = last(m.ptUsd);
      const ytUsd   = last(m.ytUsd);
      const lpUsd   = last(m.lpUsd);
      const idleUsd = last(m.idleUsd);
      const liquidityUsd = m.liquidityUsd ?? lpUsd;
      const t = ap.byTicker[ticker];
      const ptSupply = t?.legs.PT?.byMarket?.[mk]?.slice(-1)[0] || 0;
      const h = holders?.holdersByMarket?.[mk] ?? holders?.byMarketLeg?.[`${mk}:PT`]?.holders ?? 0;
      const vArr = volume?.byMarket?.[mk]?.totalUsd ?? [];
      const volume7dUsd = vArr.slice(-7).reduce((s, v) => s + (v || 0), 0);
      const mat = maturityMs(mk);
      const isActive = mat !== null ? mat >= todayMs : tvlUsd > 1 || ptSupply > 0;
      const v2m = v2?.byMarket[mk];
      if (v2m) seenV2.add(mk);
      out.push({
        marketKey: mk, ticker, platform,
        tvlUsd, ptUsd, ytUsd, lpUsd, idleUsd, liquidityUsd, volume7dUsd, ptSupply, holders: h,
        isActive, isTest: !!m.isTest,
        hasV2: !!v2m, bookCount: v2m?.bookCount ?? 0,
        oiSy: (v2m?.totalBidSizeSy ?? 0) + (v2m?.totalAskSizeSy ?? 0),
      });
    }
    // Synthetic rows for v2-only markets (no v1 entry in tvl.byMarket).
    if (v2) {
      for (const [mk, v2m] of Object.entries(v2.byMarket)) {
        if (seenV2.has(mk)) continue;
        const mat = maturityMs(mk);
        const isActive = mat !== null ? mat >= todayMs : true;
        out.push({
          marketKey: mk, ticker: v2m.ticker, platform: v2m.platform || 'Other',
          tvlUsd: 0, ptUsd: 0, ytUsd: 0, lpUsd: 0, idleUsd: 0,
          liquidityUsd: 0, volume7dUsd: 0, ptSupply: 0, holders: 0,
          isActive, isTest: false,
          hasV2: true, bookCount: v2m.bookCount,
          oiSy: (v2m.totalBidSizeSy ?? 0) + (v2m.totalAskSizeSy ?? 0),
        });
      }
    }
    const filtered = out.filter(r => {
      if (status === 'v2')     return r.hasV2;
      if (status === 'active') return r.isActive && !r.isTest;
      return true;
    });
    filtered.sort((a, b) => {
      let cmp: number;
      if (sortKey === 'marketKey') {
        const am = maturityMs(a.marketKey);
        const bm = maturityMs(b.marketKey);
        if (am === null && bm === null) cmp = a.marketKey.localeCompare(b.marketKey);
        else if (am === null) cmp = 1;
        else if (bm === null) cmp = -1;
        else cmp = am - bm;
      } else if (sortKey === 'platform') {
        cmp = a.platform.localeCompare(b.platform);
      } else {
        const av = (a as any)[sortKey];
        const bv = (b as any)[sortKey];
        cmp = (Number(av) || 0) - (Number(bv) || 0);
      }
      return sortDesc ? -cmp : cmp;
    });
    return filtered;
  }, [tvl, ap, holders, volume, v2, status, sortKey, sortDesc]);

  function toggleSort(k: SortKey) {
    if (k === sortKey) setSortDesc(d => !d);
    else { setSortKey(k); setSortDesc(true); }
  }
  const arrow = (k: SortKey) => k === sortKey ? (sortDesc ? '↓' : '↑') : '';

  if (!tvl || !ap) return <div className="text-white/40 text-sm p-4">Loading markets…</div>;

  return (
    <section className="rounded-2xl border border-white/10 bg-white/5 p-4 mb-6">
      <header className="flex items-baseline justify-between flex-wrap gap-2 mb-3">
        <div>
          <h2 className="text-sm uppercase tracking-wider text-white/60">Markets</h2>
          <p className="text-xs text-white/40">{rows.length} markets · click a row for detail</p>
        </div>
        <div className="flex items-center gap-1">
          {([
            ['active', 'Active'],
            ['all',    'All'],
            ['v2',     'v2 only'],
          ] as const).map(([s, label]) => (
            <button key={s} onClick={() => setStatus(s as StatusFilter)}
              className={`text-xs px-3 py-1 rounded-lg border ${status === s ? 'border-white/30 bg-white/10' : 'border-white/10 text-white/40'}`}>
              {label}
            </button>
          ))}
        </div>
      </header>

      <div className="overflow-x-auto">
        <table className="w-full text-xs">
          <thead className="text-white/40 border-b border-white/10">
            <tr>
              {([
                ['marketKey',    'Market',    'left',  ''],
                ['platform',     'Platform',  'left',  ''],
                ['tvlUsd',       'TVL',       'right', 'Total capital in the market\'s SY vault, USD. Counts unredeemed post-maturity capital too — matches DefiLlama methodology.'],
                ['ptUsd',        'PT',        'right', 'PT supply × market-implied PT price, USD. Independent of LP — pool-side PT is counted here AND in LP (intentional overlap, matches Exponent\'s convention).'],
                ['ytUsd',        'YT',        'right', 'YT supply × market-implied YT price, USD. Goes to 0 at maturity (yield strip exhausted).'],
                ['lpUsd',        'LP',        'right', 'AMM pool reserves valued at current prices (= SY in pool + PT in pool × discount). Same formula Exponent uses for "Liquidity". Overlaps PT by the pool-PT amount on markets with deep pools — that\'s correct, not double-counting.'],
                ['idleUsd',      'Idle',      'right', 'Vault SY not currently in user PT/YT positions or in the AMM pool — typically the post-maturity unredeemed bucket.'],
                ['liquidityUsd', 'Liquidity', 'right', "Same as LP. Kept as a separate column to mirror Exponent's UI label."],
                ['volume7dUsd',  'Vol 7d',    'right', 'Trailing 7-day total swap volume in USD'],
                ['holders',      'Holders',   'right', 'Unique wallets holding PT, YT, or LP'],
                ['oiSy',         'LO OI',     'right', 'Limit Order Open Interest — total resting-order notional in SY across both sides on the v2 orderbook (latest snapshot). Tracked separately from TVL: LO escrow is maker capital waiting to be matched, not deployed-into-positions value. The SY already counts in TVL via SY supply — this column shows the subset currently sitting as resting orders.'],
              ] as const).map(([key, label, align, tip]) => (
                <th key={key} title={tip || undefined}
                    className={`py-2 font-normal cursor-pointer select-none hover:text-white/70 ${align === 'left' ? 'text-left' : 'text-right'}`}
                    onClick={() => toggleSort(key as SortKey)}>
                  {label}<span className="ml-1 text-white/30">{arrow(key as SortKey)}</span>
                </th>
              ))}
            </tr>
          </thead>
          <tbody>
            {rows.slice(0, 100).map(r => {
              const v2Badge = r.hasV2 ? (r.bookCount > 1 ? 'v2²' : (r.tvlUsd > 0 ? 'v2' : 'v2*')) : null;
              return (
                <tr key={r.marketKey} className="border-b border-white/5 hover:bg-white/5 cursor-pointer"
                    onClick={() => window.location.href = `/market/?key=${r.marketKey}`}>
                  <td className="py-1.5 text-white/85 whitespace-nowrap">
                    {r.marketKey}
                    {v2Badge && (
                      <span className="ml-2 text-[10px] uppercase tracking-wider px-1.5 py-0.5 rounded border border-emerald-400/30 bg-emerald-400/10 text-emerald-300"
                            title={r.bookCount > 1 ? `v2 — ${r.bookCount} books on this market` : (r.tvlUsd > 0 ? 'v2 orderbook + v1 AMM' : 'v2-only (no v1 TVL)')}>
                        {v2Badge}
                      </span>
                    )}
                    {r.isTest && (
                      <span className="ml-2 text-[10px] uppercase tracking-wider px-1.5 py-0.5 rounded border border-amber-400/30 bg-amber-400/10 text-amber-300"
                            title="Test/calibration deployment">
                        test
                      </span>
                    )}
                    {!r.isActive && (
                      <span className="ml-2 text-[10px] uppercase tracking-wider px-1.5 py-0.5 rounded border border-white/20 bg-white/5 text-white/50"
                            title="Past maturity — PT redeemable 1:1 for underlying, YT has 0 yield-claim. Capital in vault until holders redeem.">
                        expired
                      </span>
                    )}
                  </td>
                  <td className="py-1.5 text-white/60">{r.platform}</td>
                  <td className="py-1.5 text-right tabular-nums text-white/80">{fmtUsd(r.tvlUsd)}</td>
                  <td className="py-1.5 text-right tabular-nums text-white/70">{r.ptUsd > 0 ? fmtUsd(r.ptUsd) : '–'}</td>
                  <td className="py-1.5 text-right tabular-nums text-white/70">{r.ytUsd > 0.5 ? fmtUsd(r.ytUsd) : '–'}</td>
                  <td className="py-1.5 text-right tabular-nums text-white/70">{r.lpUsd > 0 ? fmtUsd(r.lpUsd) : '–'}</td>
                  <td className="py-1.5 text-right tabular-nums text-white/70">{r.idleUsd > 0 ? fmtUsd(r.idleUsd) : '–'}</td>
                  <td className="py-1.5 text-right tabular-nums text-white/70">{r.liquidityUsd > 0 ? fmtUsd(r.liquidityUsd) : '–'}</td>
                  <td className="py-1.5 text-right tabular-nums text-white/70">{r.volume7dUsd > 0 ? fmtUsd(r.volume7dUsd) : '–'}</td>
                  <td className="py-1.5 text-right tabular-nums text-white/70">{r.holders || '–'}</td>
                  <td className="py-1.5 text-right tabular-nums text-white/70 whitespace-nowrap">
                    {r.hasV2 && r.oiSy > 0 ? fmtSy(r.oiSy) : '–'}
                  </td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>
      {rows.length > 100 && <div className="text-xs text-white/30 mt-2 text-center">Showing top 100 of {rows.length} markets</div>}
    </section>
  );
}
