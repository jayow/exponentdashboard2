'use client';
import { useEffect, useMemo, useState } from 'react';

type TvlByMarket = Record<string, {
  ticker: string; platform: string; tvlUsd: number[];
  ptUsd?: number[]; ytUsd?: number[]; lpUsd?: number[]; idleUsd?: number[];
  liquidityUsd?: number; isTest?: boolean;
}>;
type TvlData = { byMarket: TvlByMarket };
type Holders = {
  byMarketLeg: Record<string, { holders: number }>;
  holdersByMarket?: Record<string, number>;
};

type SortKey = 'platform' | 'tvlUsd' | 'liquidityUsd' | 'activeCount' | 'totalCount' | 'holders';

function fmtUsd(n: number) {
  if (!n) return '–';
  if (n >= 1e9) return `$${(n / 1e9).toFixed(2)}B`;
  if (n >= 1e6) return `$${(n / 1e6).toFixed(2)}M`;
  if (n >= 1e3) return `$${(n / 1e3).toFixed(1)}K`;
  return `$${n.toFixed(2)}`;
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

export function PlatformsList() {
  const [tvl, setTvl] = useState<TvlData | null>(null);
  const [holders, setHolders] = useState<Holders | null>(null);
  const [sortKey, setSortKey] = useState<SortKey>('tvlUsd');
  const [sortDesc, setSortDesc] = useState<boolean>(true);

  useEffect(() => {
    fetch('/tvl.json').then(r => r.json()).then(setTvl).catch(() => null);
    fetch('/market_holders.json').then(r => r.json()).then(setHolders).catch(() => null);
  }, []);

  const rows = useMemo(() => {
    if (!tvl) return [];
    const now = new Date();
    const todayMs = Date.UTC(now.getUTCFullYear(), now.getUTCMonth(), now.getUTCDate());

    // Aggregate per platform — TVL/liquidity sum latest values; holder
    // count unions wallet sets across all the platform's markets.
    type Agg = {
      platform: string;
      tvlUsd: number;
      liquidityUsd: number;
      activeCount: number;
      totalCount: number;
      holders: number;
      _holderSet: Set<string>;
      tickers: Set<string>;
    };
    const agg: Record<string, Agg> = {};
    const last = (arr?: number[]) => (arr && arr.length ? arr[arr.length - 1] || 0 : 0);
    for (const [mk, m] of Object.entries(tvl.byMarket)) {
      if (mk.includes('(unsplit)')) continue;
      if (m.isTest) continue;
      const platform = m.platform || 'Other';
      const a = agg[platform] ??= {
        platform, tvlUsd: 0, liquidityUsd: 0, activeCount: 0, totalCount: 0,
        holders: 0, _holderSet: new Set(), tickers: new Set(),
      };
      a.tvlUsd       += last(m.tvlUsd);
      a.liquidityUsd += m.liquidityUsd ?? 0;
      a.totalCount   += 1;
      a.tickers.add(m.ticker);
      const mat = maturityMs(mk);
      if (mat !== null && mat >= todayMs) a.activeCount += 1;
    }
    // Per-market unique holder count is in holders.holdersByMarket — sum
    // per platform conservatively (overstates because same wallet across
    // markets counts multiple times). For a true cross-market dedupe
    // we'd need wallet lists, which we don't surface here.
    if (holders?.holdersByMarket) {
      for (const [mk, n] of Object.entries(holders.holdersByMarket)) {
        const m = tvl.byMarket[mk];
        if (!m || mk.includes('(unsplit)') || m.isTest) continue;
        const platform = m.platform || 'Other';
        if (agg[platform]) agg[platform].holders += n;
      }
    }
    const out = Object.values(agg);
    out.sort((a, b) => {
      let cmp: number;
      if (sortKey === 'platform') cmp = a.platform.localeCompare(b.platform);
      else cmp = (a[sortKey] || 0) - (b[sortKey] || 0);
      return sortDesc ? -cmp : cmp;
    });
    return out;
  }, [tvl, holders, sortKey, sortDesc]);

  function toggleSort(k: SortKey) {
    if (k === sortKey) setSortDesc(d => !d);
    else { setSortKey(k); setSortDesc(true); }
  }
  const arrow = (k: SortKey) => k === sortKey ? (sortDesc ? '↓' : '↑') : '';

  if (!tvl) return <div className="text-white/40 text-sm p-4">Loading platforms…</div>;

  const totalTvl = rows.reduce((s, r) => s + r.tvlUsd, 0);

  return (
    <section className="rounded-2xl border border-white/10 bg-white/5 p-4 mb-6">
      <header className="flex items-baseline justify-between flex-wrap gap-2 mb-3">
        <div>
          <h2 className="text-sm uppercase tracking-wider text-white/60">Platforms</h2>
          <p className="text-xs text-white/40">{rows.length} platforms • aggregated across each platform's markets</p>
        </div>
      </header>
      <div className="overflow-x-auto">
        <table className="w-full text-xs">
          <thead className="text-white/40 border-b border-white/10">
            <tr>
              {([
                ['platform',     'Platform',         'left',  ''],
                ['activeCount',  'Active markets',   'right', 'Markets whose maturity is on or after today'],
                ['totalCount',   'Total markets',    'right', 'All markets (active + expired) under this platform'],
                ['tvlUsd',       'TVL',              'right', 'Sum of per-market TVL across this platform'],
                ['liquidityUsd', 'Liquidity',        'right', 'Sum of per-market AMM pool TVL (Exponent UI Liquidity)'],
                ['holders',      'Holders (sum)',    'right', 'Sum of per-market unique holders. Wallets active in multiple markets are counted multiple times.'],
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
            {rows.map(r => {
              const share = totalTvl > 0 ? (r.tvlUsd / totalTvl) * 100 : 0;
              return (
                <tr key={r.platform} className="border-b border-white/5 hover:bg-white/5">
                  <td className="py-1.5 text-white/85">
                    {r.platform}
                    <span className="ml-2 text-white/30 text-[10px]">{Array.from(r.tickers).slice(0, 4).join(' · ')}{r.tickers.size > 4 ? ` +${r.tickers.size - 4}` : ''}</span>
                  </td>
                  <td className="py-1.5 text-right tabular-nums text-white/70">{r.activeCount || '–'}</td>
                  <td className="py-1.5 text-right tabular-nums text-white/70">{r.totalCount}</td>
                  <td className="py-1.5 text-right tabular-nums text-white/80">
                    {fmtUsd(r.tvlUsd)}
                    <span className="ml-1.5 text-white/30 text-[10px]">{share.toFixed(1)}%</span>
                  </td>
                  <td className="py-1.5 text-right tabular-nums text-white/70">{fmtUsd(r.liquidityUsd)}</td>
                  <td className="py-1.5 text-right tabular-nums text-white/70">{r.holders || '–'}</td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>
    </section>
  );
}
