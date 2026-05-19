'use client';
import { useEffect, useMemo, useState } from 'react';
import {
  AreaChart, Area, BarChart, Bar, XAxis, YAxis, Tooltip, ResponsiveContainer, TooltipProps,
} from 'recharts';
import { colorForPlatform, colorForMarket, platformOfTicker } from '@/lib/colors';

type TvlData = {
  dates: string[];
  protocolUsd: number[];
  protocolPrincipalUsd: number[];
  decomposition: { principalPt: number[]; liquidityLp: number[]; idle: number[] };
  byPlatform: Record<string, number[]>;
  byMarket: Record<string, { ticker: string; platform: string; tvlUsd: number[] }>;
};
type VolData = {
  dates: string[];
  protocol: { totalUsd: number[]; ptUsd: number[]; ytUsd: number[] };
  byPlatform: Record<string, number[]>;
  byMarket: Record<string, { ticker: string; totalUsd: number[]; ptUsd: number[]; ytUsd: number[] }>;
};
type ApData = {
  dates: string[];
  byTicker: Record<string, {
    underlyingMint: string;
    latest: Record<string, number>;
    legs: Record<string, { byMarket: Record<string, number[]>; totals: number[] }>;
  }>;
};

type Metric = 'tvl' | 'volume' | 'positions' | 'breakdown';
type View = 'protocol' | 'platform' | 'market';
type Range = '30d' | '90d' | '1y' | 'all';

const TOP_N = 10;
// Breakdown-only colors (PT / LP / Idle / etc.) — not platform-tied
const BREAKDOWN_COLOR: Record<string, string> = {
  'Principal (PT)': '#a78bfa',
  'Liquidity (LP)': '#4ade80',
  'Idle':           '#9ca3af',
  'PT':             '#a78bfa',
  'LP':             '#4ade80',
  'Idle (SY)':      '#9ca3af',
  'TVL':            '#a78bfa',
  'Volume':         '#38bdf8',
  'Other':          '#666',
};

function fmtUsd(n: number) {
  if (Math.abs(n) >= 1e9) return `$${(n / 1e9).toFixed(2)}B`;
  if (Math.abs(n) >= 1e6) return `$${(n / 1e6).toFixed(2)}M`;
  if (Math.abs(n) >= 1e3) return `$${(n / 1e3).toFixed(1)}K`;
  return `$${n.toFixed(0)}`;
}
function rangeStart(dates: string[], range: Range) {
  if (range === 'all') return 0;
  const days = range === '30d' ? 30 : range === '90d' ? 90 : 365;
  const cutoff = new Date(new Date(dates[dates.length - 1]).getTime() - days * 86400_000).toISOString().slice(0, 10);
  return Math.max(0, dates.findIndex(d => d >= cutoff));
}

function SortedTooltip({ active, payload, label }: TooltipProps<number, string>) {
  if (!active || !payload?.length) return null;
  const entries = payload
    .map(p => ({ name: String(p.name ?? p.dataKey ?? ''), value: typeof p.value === 'number' ? p.value : 0, color: p.color || (p as any).fill || '#888' }))
    .filter(e => Math.abs(e.value) > 0.01)
    .sort((a, b) => Math.abs(b.value) - Math.abs(a.value));
  if (!entries.length) return null;
  const total = entries.reduce((s, e) => s + e.value, 0);
  return (
    <div className="bg-[#0a0a0a] border border-white/15 rounded-lg p-2 text-xs shadow-xl min-w-[200px]">
      <div className="text-white/70 mb-1 font-medium">{label}</div>
      <div className="space-y-0.5">
        {entries.map(e => (
          <div key={e.name} className="flex justify-between gap-3 items-center">
            <span className="flex items-center gap-1.5 text-white/85 truncate">
              <span className="w-2 h-2 rounded-sm" style={{ backgroundColor: e.color }} />
              <span className="truncate">{e.name}</span>
            </span>
            <span className="tabular-nums text-white">
              {fmtUsd(e.value)}
              {entries.length > 1 && total !== 0 && <span className="text-white/40 ml-1.5">({((e.value/total)*100).toFixed(0)}%)</span>}
            </span>
          </div>
        ))}
      </div>
      {entries.length > 1 && (
        <div className="flex justify-between mt-1.5 pt-1.5 border-t border-white/10">
          <span className="text-white/40">Total</span>
          <span className="tabular-nums text-white/90 font-medium">{fmtUsd(total)}</span>
        </div>
      )}
    </div>
  );
}

export function BigChart() {
  const [tvl, setTvl] = useState<TvlData | null>(null);
  const [vol, setVol] = useState<VolData | null>(null);
  const [ap, setAp] = useState<ApData | null>(null);
  const [metric, setMetric] = useState<Metric>('tvl');
  const [view, setView] = useState<View>('protocol');
  const [range, setRange] = useState<Range>('all');

  useEffect(() => {
    fetch('/tvl.json').then(r => r.json()).then(setTvl).catch(() => null);
    fetch('/volume.json').then(r => r.json()).then(setVol).catch(() => null);
    fetch('/active_positions.json').then(r => r.json()).then(setAp).catch(() => null);
  }, []);

  // Breakdown isn't compatible with platform/market view — force protocol
  const effectiveView = metric === 'breakdown' ? 'protocol' : view;

  const dates = metric === 'volume' ? vol?.dates : tvl?.dates;
  const start = dates ? rangeStart(dates, range) : 0;

  // Build chart series keys + data + color-for-key map based on (metric, view).
  // For platform/market views, top-N + Other tail is collapsed into one bucket
  // so we never spray 30 series across the chart.
  const { data, keys, colorMap } = useMemo(() => {
    type Result = { data: any[]; keys: string[]; colorMap: Record<string, string> };
    const empty: Result = { data: [], keys: [], colorMap: {} };
    if (!dates) return empty;
    const sliced = dates.slice(start);

    if (metric === 'breakdown' && tvl) {
      const d = tvl.decomposition;
      const keys = ['Principal (PT)', 'Liquidity (LP)', 'Idle'];
      return {
        keys,
        colorMap: Object.fromEntries(keys.map(k => [k, BREAKDOWN_COLOR[k]])),
        data: sliced.map((dt, i) => ({
          date: dt,
          'Principal (PT)': d.principalPt[start + i] || 0,
          'Liquidity (LP)': d.liquidityLp[start + i] || 0,
          'Idle':           d.idle[start + i] || 0,
        })),
      };
    }

    // Helper: collapse a {key → series} dict into top-N by latest value
    // (or lifetime sum), with the rest summed into "Other".
    function topNCollapse(
      seriesByKey: Record<string, number[]>,
      sortBy: 'latest' | 'sum',
    ): { sortedKeys: string[]; rows: any[] } {
      const entries = Object.entries(seriesByKey).map(([k, s]) => ({
        k,
        sortVal: sortBy === 'latest' ? (s[s.length - 1] || 0) : s.reduce((a, b) => a + (b || 0), 0),
        series: s,
      })).sort((a, b) => b.sortVal - a.sortVal);
      const top = entries.slice(0, TOP_N).filter(e => e.sortVal > 0);
      const rest = entries.slice(TOP_N);
      const sortedKeys = top.map(e => e.k);
      if (rest.length) sortedKeys.push('Other');
      const rows = sliced.map((dt, i) => {
        const row: any = { date: dt };
        for (const e of top) row[e.k] = e.series[start + i] || 0;
        if (rest.length) {
          let other = 0;
          for (const e of rest) other += e.series[start + i] || 0;
          row['Other'] = other;
        }
        return row;
      });
      return { sortedKeys, rows };
    }

    if (metric === 'tvl' && tvl) {
      if (effectiveView === 'protocol') {
        return {
          keys: ['TVL'],
          colorMap: { TVL: BREAKDOWN_COLOR['TVL'] },
          data: sliced.map((dt, i) => ({ date: dt, TVL: tvl.protocolUsd[start + i] || 0 })),
        };
      }
      if (effectiveView === 'platform') {
        const { sortedKeys, rows } = topNCollapse(tvl.byPlatform, 'latest');
        return {
          keys: sortedKeys, data: rows,
          colorMap: Object.fromEntries(sortedKeys.map(k => [k, k === 'Other' ? BREAKDOWN_COLOR['Other'] : colorForPlatform(k)])),
        };
      }
      // market view: top-N markets by latest TVL, shade within platform
      const seriesByMk: Record<string, number[]> = {};
      const platformForMk: Record<string, string> = {};
      for (const [mk, m] of Object.entries(tvl.byMarket)) {
        seriesByMk[mk] = m.tvlUsd;
        platformForMk[mk] = m.platform || platformOfTicker(m.ticker);
      }
      const { sortedKeys, rows } = topNCollapse(seriesByMk, 'latest');
      // Build per-platform index so shades within a platform differ
      const seen: Record<string, number> = {};
      const colorMap: Record<string, string> = {};
      for (const k of sortedKeys) {
        if (k === 'Other') { colorMap[k] = BREAKDOWN_COLOR['Other']; continue; }
        const p = platformForMk[k] || 'Other';
        const i = seen[p] || 0;
        colorMap[k] = colorForMarket(p, i);
        seen[p] = i + 1;
      }
      return { keys: sortedKeys, data: rows, colorMap };
    }

    if (metric === 'volume' && vol) {
      if (effectiveView === 'protocol') {
        return {
          keys: ['Volume'],
          colorMap: { Volume: BREAKDOWN_COLOR['Volume'] },
          data: sliced.map((dt, i) => ({ date: dt, Volume: vol.protocol.totalUsd[start + i] || 0 })),
        };
      }
      if (effectiveView === 'platform') {
        const { sortedKeys, rows } = topNCollapse(vol.byPlatform, 'sum');
        return {
          keys: sortedKeys, data: rows,
          colorMap: Object.fromEntries(sortedKeys.map(k => [k, k === 'Other' ? BREAKDOWN_COLOR['Other'] : colorForPlatform(k)])),
        };
      }
      const seriesByMk: Record<string, number[]> = {};
      const platformForMk: Record<string, string> = {};
      for (const [mk, m] of Object.entries(vol.byMarket)) {
        seriesByMk[mk] = m.totalUsd;
        platformForMk[mk] = platformOfTicker(m.ticker);
      }
      const { sortedKeys, rows } = topNCollapse(seriesByMk, 'sum');
      const seen: Record<string, number> = {};
      const colorMap: Record<string, string> = {};
      for (const k of sortedKeys) {
        if (k === 'Other') { colorMap[k] = BREAKDOWN_COLOR['Other']; continue; }
        const p = platformForMk[k] || 'Other';
        const i = seen[p] || 0;
        colorMap[k] = colorForMarket(p, i);
        seen[p] = i + 1;
      }
      return { keys: sortedKeys, data: rows, colorMap };
    }

    if (metric === 'positions' && tvl) {
      const d = tvl.decomposition;
      const keys = ['PT', 'LP', 'Idle (SY)'];
      return {
        keys,
        colorMap: Object.fromEntries(keys.map(k => [k, BREAKDOWN_COLOR[k]])),
        data: sliced.map((dt, i) => ({
          date: dt,
          PT:        d.principalPt[start + i] || 0,
          LP:        d.liquidityLp[start + i] || 0,
          'Idle (SY)': d.idle[start + i] || 0,
        })),
      };
    }

    return empty;
  }, [tvl, vol, ap, metric, effectiveView, range, start, dates]);

  if (!tvl || !vol || !ap) return <div className="text-white/40 text-sm p-4">Loading chart…</div>;

  // Chart type: Volume is bar; everything else is area-stacked
  const isBar = metric === 'volume';
  const isStacked = keys.length > 1;

  return (
    <section className="rounded-2xl border border-white/10 bg-white/5 p-4 mb-6">
      <header className="flex items-center justify-between flex-wrap gap-3 mb-3">
        <div className="flex items-center gap-1 flex-wrap">
          {(['tvl', 'volume', 'positions', 'breakdown'] as Metric[]).map(m => (
            <button key={m} onClick={() => setMetric(m)}
              className={`text-xs px-3 py-1.5 rounded-lg border ${metric === m ? 'border-white/30 bg-white/10 text-white' : 'border-white/10 text-white/40 hover:text-white'}`}>
              {m === 'tvl' ? 'TVL' : m === 'volume' ? 'Volume' : m === 'positions' ? 'Positions' : 'Breakdown'}
            </button>
          ))}
          <span className="w-3" />
          <span className="text-[11px] text-white/30">View:</span>
          {(['protocol', 'platform', 'market'] as View[]).map(v => {
            const disabled = metric === 'breakdown' && v !== 'protocol';
            return (
              <button key={v} onClick={() => !disabled && setView(v)} disabled={disabled}
                className={`text-xs px-3 py-1 rounded-lg border ${disabled ? 'border-white/5 text-white/20 cursor-not-allowed' : effectiveView === v ? 'border-white/30 bg-white/10 text-white' : 'border-white/10 text-white/40 hover:text-white'}`}>
                {v.charAt(0).toUpperCase() + v.slice(1)}
              </button>
            );
          })}
        </div>
        <div className="flex items-center gap-1">
          {(['30d', '90d', '1y', 'all'] as Range[]).map(r => (
            <button key={r} onClick={() => setRange(r)}
              className={`text-xs px-2.5 py-1 rounded-md ${range === r ? 'bg-white/10 text-white' : 'text-white/30 hover:text-white/60'}`}>
              {r === 'all' ? 'All' : r.toUpperCase()}
            </button>
          ))}
        </div>
      </header>

      <ResponsiveContainer width="100%" height={360}>
        {isBar ? (
          <BarChart data={data} margin={{ top: 10, right: 10, left: 10, bottom: 0 }}>
            <XAxis dataKey="date" tick={{ fill: '#888', fontSize: 11 }} axisLine={false} tickLine={false} />
            <YAxis tick={{ fill: '#888', fontSize: 11 }} tickFormatter={fmtUsd} axisLine={false} tickLine={false} />
            <Tooltip content={<SortedTooltip />} />
            {keys.map(k => (
              <Bar key={k} dataKey={k} fill={colorMap[k] ?? '#666'} fillOpacity={0.9}
                   stackId={isStacked ? 's' : undefined} />
            ))}
          </BarChart>
        ) : (
          <AreaChart data={data} margin={{ top: 10, right: 10, left: 10, bottom: 0 }}>
            <XAxis dataKey="date" tick={{ fill: '#888', fontSize: 11 }} axisLine={false} tickLine={false} />
            <YAxis tick={{ fill: '#888', fontSize: 11 }} tickFormatter={fmtUsd} axisLine={false} tickLine={false} />
            <Tooltip content={<SortedTooltip />} />
            {keys.map(k => {
              const c = colorMap[k] ?? '#666';
              return (
                <Area key={k} type="monotone" dataKey={k}
                      stackId={isStacked ? 's' : undefined}
                      stroke={c} fill={c + '66'} />
              );
            })}
          </AreaChart>
        )}
      </ResponsiveContainer>
    </section>
  );
}
