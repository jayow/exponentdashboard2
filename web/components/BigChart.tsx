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

const DEFAULT_TOP = 15;

const BREAKDOWN_COLOR: Record<string, string> = {
  'Principal (PT)': '#a78bfa',
  'Liquidity (LP)': '#4ade80',
  'Idle':           '#9ca3af',
  'PT':             '#a78bfa',
  'LP':             '#4ade80',
  'Idle (SY)':      '#9ca3af',
  'TVL':            '#a78bfa',
  'Volume':         '#38bdf8',
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
    <div className="bg-[#0a0a0a] border border-white/15 rounded-lg p-2 text-xs shadow-xl min-w-[220px] max-h-[360px] overflow-y-auto">
      <div className="text-white/70 mb-1 font-medium">{label}</div>
      <div className="space-y-0.5">
        {entries.map(e => (
          <div key={e.name} className="flex justify-between gap-3 items-center">
            <span className="flex items-center gap-1.5 text-white/85 truncate">
              <span className="w-2 h-2 rounded-sm shrink-0" style={{ backgroundColor: e.color }} />
              <span className="truncate">{e.name}</span>
            </span>
            <span className="tabular-nums text-white shrink-0">
              {fmtUsd(e.value)}
              {entries.length > 1 && total !== 0 && <span className="text-white/40 ml-1.5">({((e.value/total)*100).toFixed(0)}%)</span>}
            </span>
          </div>
        ))}
      </div>
      {entries.length > 1 && (
        <div className="flex justify-between mt-1.5 pt-1.5 border-t border-white/10 sticky bottom-0 bg-[#0a0a0a]">
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
  const [hidden, setHidden] = useState<Set<string>>(new Set());

  useEffect(() => {
    fetch('/tvl.json').then(r => r.json()).then(setTvl).catch(() => null);
    fetch('/volume.json').then(r => r.json()).then(setVol).catch(() => null);
    fetch('/active_positions.json').then(r => r.json()).then(setAp).catch(() => null);
  }, []);

  // Breakdown isn't compatible with platform/market view — force protocol
  const effectiveView = metric === 'breakdown' ? 'protocol' : view;

  const dates = metric === 'volume' ? vol?.dates : tvl?.dates;
  const start = dates ? rangeStart(dates, range) : 0;

  // Build full series for all keys (no Other collapse) + a color map.
  // Hide/show is applied at render time.
  const { allKeys, data, colorMap, isFlat, breakdownLike } = useMemo(() => {
    type Result = {
      allKeys: string[]; data: any[]; colorMap: Record<string, string>;
      isFlat: boolean;          // single-series chart (no stacking, no legend toggling)
      breakdownLike: boolean;   // breakdown/positions — fixed key list
    };
    const empty: Result = { allKeys: [], data: [], colorMap: {}, isFlat: true, breakdownLike: false };
    if (!dates) return empty;
    const sliced = dates.slice(start);

    if (metric === 'breakdown' && tvl) {
      const d = tvl.decomposition;
      const keys = ['Principal (PT)', 'Liquidity (LP)', 'Idle'];
      return {
        allKeys: keys, isFlat: false, breakdownLike: true,
        colorMap: Object.fromEntries(keys.map(k => [k, BREAKDOWN_COLOR[k]])),
        data: sliced.map((dt, i) => ({
          date: dt,
          'Principal (PT)': d.principalPt[start + i] || 0,
          'Liquidity (LP)': d.liquidityLp[start + i] || 0,
          'Idle':           d.idle[start + i] || 0,
        })),
      };
    }

    function emitFromSeries(seriesByKey: Record<string, number[]>, sortBy: 'latest' | 'sum', isMarket: boolean, platformResolver: (k: string) => string) {
      const entries = Object.entries(seriesByKey).map(([k, s]) => ({
        k,
        sortVal: sortBy === 'latest' ? (s[s.length - 1] || 0) : s.reduce((a, b) => a + (b || 0), 0),
        series: s,
      })).filter(e => e.sortVal > 0).sort((a, b) => b.sortVal - a.sortVal);
      const allKeys = entries.map(e => e.k);
      const rows = sliced.map((dt, i) => {
        const row: any = { date: dt };
        for (const e of entries) row[e.k] = e.series[start + i] || 0;
        return row;
      });
      const seen: Record<string, number> = {};
      const colorMap: Record<string, string> = {};
      for (const k of allKeys) {
        if (!isMarket) {
          colorMap[k] = colorForPlatform(k);
        } else {
          const p = platformResolver(k) || 'Other';
          const i = seen[p] || 0;
          colorMap[k] = colorForMarket(p, i);
          seen[p] = i + 1;
        }
      }
      return { allKeys, data: rows, colorMap, isFlat: false, breakdownLike: false };
    }

    if (metric === 'tvl' && tvl) {
      if (effectiveView === 'protocol') {
        return {
          allKeys: ['TVL'], isFlat: true, breakdownLike: false,
          colorMap: { TVL: BREAKDOWN_COLOR['TVL'] },
          data: sliced.map((dt, i) => ({ date: dt, TVL: tvl.protocolUsd[start + i] || 0 })),
        };
      }
      if (effectiveView === 'platform') {
        return emitFromSeries(tvl.byPlatform, 'latest', false, () => '');
      }
      // market
      const seriesByMk: Record<string, number[]> = {};
      const platformForMk: Record<string, string> = {};
      for (const [mk, m] of Object.entries(tvl.byMarket)) {
        seriesByMk[mk] = m.tvlUsd;
        platformForMk[mk] = m.platform || platformOfTicker(m.ticker);
      }
      return emitFromSeries(seriesByMk, 'latest', true, (k: string) => platformForMk[k]);
    }

    if (metric === 'volume' && vol) {
      if (effectiveView === 'protocol') {
        return {
          allKeys: ['Volume'], isFlat: true, breakdownLike: false,
          colorMap: { Volume: BREAKDOWN_COLOR['Volume'] },
          data: sliced.map((dt, i) => ({ date: dt, Volume: vol.protocol.totalUsd[start + i] || 0 })),
        };
      }
      if (effectiveView === 'platform') {
        return emitFromSeries(vol.byPlatform, 'sum', false, () => '');
      }
      const seriesByMk: Record<string, number[]> = {};
      const platformForMk: Record<string, string> = {};
      for (const [mk, m] of Object.entries(vol.byMarket)) {
        seriesByMk[mk] = m.totalUsd;
        platformForMk[mk] = platformOfTicker(m.ticker);
      }
      return emitFromSeries(seriesByMk, 'sum', true, (k: string) => platformForMk[k]);
    }

    if (metric === 'positions' && tvl) {
      const d = tvl.decomposition;
      const keys = ['PT', 'LP', 'Idle (SY)'];
      return {
        allKeys: keys, isFlat: false, breakdownLike: true,
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

  // When metric/view changes, reset the hidden set to the *default* — show
  // top 15 by lifetime contribution, hide the long tail. Stacked-fixed-key
  // views (breakdown/positions/protocol) always show all.
  useEffect(() => {
    if (isFlat || breakdownLike) { setHidden(new Set()); return; }
    if (allKeys.length <= DEFAULT_TOP) { setHidden(new Set()); return; }
    setHidden(new Set(allKeys.slice(DEFAULT_TOP)));
  }, [allKeys, isFlat, breakdownLike]);

  const visibleKeys = useMemo(() => allKeys.filter(k => !hidden.has(k)), [allKeys, hidden]);

  // Toggle / All / None / Top-15 helpers
  function toggle(k: string) {
    setHidden(prev => {
      const next = new Set(prev);
      if (next.has(k)) next.delete(k); else next.add(k);
      return next;
    });
  }
  const showAll  = () => setHidden(new Set());
  const showNone = () => setHidden(new Set(allKeys));
  const showTop  = (n: number) => setHidden(new Set(allKeys.slice(n)));

  if (!tvl || !vol || !ap) return <div className="text-white/40 text-sm p-4">Loading chart…</div>;

  const isBar = metric === 'volume';
  const isStacked = visibleKeys.length > 1;
  const isToggleable = !isFlat && !breakdownLike;

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
            <Tooltip content={<SortedTooltip />} cursor={{ fill: 'rgba(255,255,255,0.04)' }} />
            {visibleKeys.map(k => (
              <Bar key={k} dataKey={k} fill={colorMap[k] ?? '#9ca3af'} fillOpacity={0.9}
                   stackId={isStacked ? 's' : undefined} />
            ))}
          </BarChart>
        ) : (
          <AreaChart data={data} margin={{ top: 10, right: 10, left: 10, bottom: 0 }}>
            <XAxis dataKey="date" tick={{ fill: '#888', fontSize: 11 }} axisLine={false} tickLine={false} />
            <YAxis tick={{ fill: '#888', fontSize: 11 }} tickFormatter={fmtUsd} axisLine={false} tickLine={false} />
            <Tooltip content={<SortedTooltip />} />
            {visibleKeys.map(k => {
              const c = colorMap[k] ?? '#9ca3af';
              return (
                <Area key={k} type="monotone" dataKey={k}
                      stackId={isStacked ? 's' : undefined}
                      stroke={c} fill={c + '66'} />
              );
            })}
          </AreaChart>
        )}
      </ResponsiveContainer>

      {/* Interactive legend — only for views where toggling makes sense */}
      {isToggleable && allKeys.length > 0 && (
        <div className="mt-3 border-t border-white/10 pt-3">
          <div className="flex items-center justify-between mb-2 flex-wrap gap-2">
            <div className="text-[11px] text-white/40">
              {visibleKeys.length} of {allKeys.length} visible
              {allKeys.length > DEFAULT_TOP && hidden.size > 0 && (
                <span className="text-white/30"> · click to toggle</span>
              )}
            </div>
            <div className="flex items-center gap-1">
              <button onClick={showAll}
                className={`text-[11px] px-2 py-0.5 rounded border ${hidden.size === 0 ? 'border-white/30 bg-white/10 text-white' : 'border-white/10 text-white/40 hover:text-white'}`}>
                All
              </button>
              <button onClick={showNone}
                className={`text-[11px] px-2 py-0.5 rounded border ${hidden.size === allKeys.length ? 'border-white/30 bg-white/10 text-white' : 'border-white/10 text-white/40 hover:text-white'}`}>
                None
              </button>
              {allKeys.length > 10 && (
                <button onClick={() => showTop(10)}
                  className="text-[11px] px-2 py-0.5 rounded border border-white/10 text-white/40 hover:text-white">
                  Top 10
                </button>
              )}
              {allKeys.length > 15 && (
                <button onClick={() => showTop(15)}
                  className="text-[11px] px-2 py-0.5 rounded border border-white/10 text-white/40 hover:text-white">
                  Top 15
                </button>
              )}
            </div>
          </div>
          <div className="flex flex-wrap gap-1.5 max-h-[110px] overflow-y-auto">
            {allKeys.map(k => {
              const isHidden = hidden.has(k);
              return (
                <button key={k} onClick={() => toggle(k)}
                  className={`text-[11px] px-2 py-0.5 rounded border transition ${
                    isHidden
                      ? 'border-white/5 bg-transparent text-white/30'
                      : 'border-white/15 bg-white/5 text-white/85 hover:bg-white/10'
                  }`}>
                  <span className="inline-block w-2 h-2 rounded-sm mr-1 align-middle"
                        style={{ backgroundColor: colorMap[k], opacity: isHidden ? 0.3 : 1 }} />
                  <span className={isHidden ? 'line-through' : ''}>{k}</span>
                </button>
              );
            })}
          </div>
        </div>
      )}
    </section>
  );
}
