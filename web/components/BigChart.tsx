'use client';
import { useEffect, useMemo, useState } from 'react';
import {
  AreaChart, Area, BarChart, Bar, XAxis, YAxis, Tooltip, ResponsiveContainer, TooltipProps,
} from 'recharts';

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

const COLORS = ['#6b66ff', '#ffb74d', '#4ade80', '#f87171', '#38bdf8',
                '#a78bfa', '#fb923c', '#34d399', '#f472b6', '#facc15',
                '#818cf8', '#fbbf24', '#22d3ee', '#e879f9', '#a3e635'];

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
  const [topN, setTopN] = useState<number>(10);

  useEffect(() => {
    fetch('/tvl.json').then(r => r.json()).then(setTvl).catch(() => null);
    fetch('/volume.json').then(r => r.json()).then(setVol).catch(() => null);
    fetch('/active_positions.json').then(r => r.json()).then(setAp).catch(() => null);
  }, []);

  // Breakdown isn't compatible with platform/market view — force protocol
  const effectiveView = metric === 'breakdown' ? 'protocol' : view;

  const dates = metric === 'volume' ? vol?.dates : tvl?.dates;
  const start = dates ? rangeStart(dates, range) : 0;

  // Build chart series keys + data shape based on (metric, view)
  const { data, keys } = useMemo(() => {
    if (!dates) return { data: [] as any[], keys: [] as string[] };
    const sliced = dates.slice(start);

    if (metric === 'breakdown' && tvl) {
      const d = tvl.decomposition;
      return {
        keys: ['Principal (PT)', 'Liquidity (LP)', 'Idle'],
        data: sliced.map((dt, i) => ({
          date: dt,
          'Principal (PT)': d.principalPt[start + i] || 0,
          'Liquidity (LP)': d.liquidityLp[start + i] || 0,
          'Idle':           d.idle[start + i] || 0,
        })),
      };
    }

    if (metric === 'tvl' && tvl) {
      if (effectiveView === 'protocol') {
        return { keys: ['TVL'], data: sliced.map((dt, i) => ({ date: dt, TVL: tvl.protocolUsd[start + i] || 0 })) };
      }
      if (effectiveView === 'platform') {
        const platforms = Object.keys(tvl.byPlatform);
        return {
          keys: platforms,
          data: sliced.map((dt, i) => {
            const row: any = { date: dt };
            for (const p of platforms) row[p] = tvl.byPlatform[p][start + i] || 0;
            return row;
          }),
        };
      }
      // market view: top-N markets by latest TVL
      const sorted = Object.entries(tvl.byMarket)
        .map(([mk, m]) => ({ mk, tvl: m.tvlUsd[m.tvlUsd.length - 1] || 0 }))
        .sort((a, b) => b.tvl - a.tvl).slice(0, topN);
      const mks = sorted.map(s => s.mk);
      return {
        keys: mks,
        data: sliced.map((dt, i) => {
          const row: any = { date: dt };
          for (const mk of mks) row[mk] = tvl.byMarket[mk].tvlUsd[start + i] || 0;
          return row;
        }),
      };
    }

    if (metric === 'volume' && vol) {
      if (effectiveView === 'protocol') {
        return { keys: ['Volume'], data: sliced.map((dt, i) => ({ date: dt, Volume: vol.protocol.totalUsd[start + i] || 0 })) };
      }
      if (effectiveView === 'platform') {
        const platforms = Object.keys(vol.byPlatform);
        return {
          keys: platforms,
          data: sliced.map((dt, i) => {
            const row: any = { date: dt };
            for (const p of platforms) row[p] = vol.byPlatform[p][start + i] || 0;
            return row;
          }),
        };
      }
      const sorted = Object.entries(vol.byMarket)
        .map(([mk, m]) => ({ mk, v: m.totalUsd.reduce((s, x) => s + (x || 0), 0) }))
        .sort((a, b) => b.v - a.v).slice(0, topN);
      const mks = sorted.map(s => s.mk);
      return {
        keys: mks,
        data: sliced.map((dt, i) => {
          const row: any = { date: dt };
          for (const mk of mks) row[mk] = vol.byMarket[mk].totalUsd[start + i] || 0;
          return row;
        }),
      };
    }

    if (metric === 'positions' && ap && tvl) {
      // Sum PT (USD) across all markets, mirroring v2 active-positions methodology.
      // For protocol view: stack PT + YT + LP (in USD using underlying price).
      // Without precomputed USD per leg, we approximate using TVL decomposition.
      const d = tvl.decomposition;
      return {
        keys: ['PT', 'LP', 'Idle (SY)'],
        data: sliced.map((dt, i) => ({
          date: dt,
          PT:        d.principalPt[start + i] || 0,
          LP:        d.liquidityLp[start + i] || 0,
          'Idle (SY)': d.idle[start + i] || 0,
        })),
      };
    }

    return { data: [], keys: [] };
  }, [tvl, vol, ap, metric, effectiveView, range, topN, start, dates]);

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
          {effectiveView === 'market' && (
            <>
              <span className="text-[11px] text-white/30 ml-2">Top</span>
              {[5, 10, 15].map(n => (
                <button key={n} onClick={() => setTopN(n)}
                  className={`text-xs px-2 py-1 rounded-lg border ${topN === n ? 'border-white/30 bg-white/10 text-white' : 'border-white/10 text-white/40'}`}>
                  {n}
                </button>
              ))}
            </>
          )}
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
            {keys.map((k, i) => (
              <Bar key={k} dataKey={k} fill={COLORS[i % COLORS.length]} fillOpacity={0.85}
                   stackId={isStacked ? 's' : undefined} />
            ))}
          </BarChart>
        ) : (
          <AreaChart data={data} margin={{ top: 10, right: 10, left: 10, bottom: 0 }}>
            <XAxis dataKey="date" tick={{ fill: '#888', fontSize: 11 }} axisLine={false} tickLine={false} />
            <YAxis tick={{ fill: '#888', fontSize: 11 }} tickFormatter={fmtUsd} axisLine={false} tickLine={false} />
            <Tooltip content={<SortedTooltip />} />
            {keys.map((k, i) => (
              <Area key={k} type="monotone" dataKey={k}
                    stackId={isStacked ? 's' : undefined}
                    stroke={COLORS[i % COLORS.length]} fill={COLORS[i % COLORS.length] + '66'} />
            ))}
          </AreaChart>
        )}
      </ResponsiveContainer>
    </section>
  );
}
