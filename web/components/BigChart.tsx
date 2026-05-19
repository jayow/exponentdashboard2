'use client';
import { useEffect, useMemo, useState } from 'react';
import {
  BarChart, Bar, XAxis, YAxis, Tooltip, ResponsiveContainer, ReferenceLine, TooltipProps,
} from 'recharts';
import { colorForPlatform, colorForMarket, platformOfTicker } from '@/lib/colors';

type TgeMarker = { platform: string; date: string };
type TvlData = {
  dates: string[];
  protocolUsd: number[];
  protocolPrincipalUsd: number[];
  decomposition: { principalPt: number[]; liquidityLp: number[]; idle: number[] };
  byPlatform: Record<string, number[]>;
  byMarket: Record<string, { ticker: string; platform: string; tvlUsd: number[] }>;
  tgeMarkers?: TgeMarker[];
};
type VolData = {
  dates: string[];
  protocol: { totalUsd: number[]; ptUsd: number[]; ytUsd: number[] };
  byPlatform: Record<string, number[]>;
  byMarket: Record<string, { ticker: string; totalUsd: number[]; ptUsd: number[]; ytUsd: number[] }>;
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
function fmtMonth(s: string) {
  const d = new Date(s + 'T00:00:00Z');
  return `${d.toLocaleString('en', { month: 'short' })} ${String(d.getUTCFullYear()).slice(-2)}`;
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
    <div className="bg-[#0a0a0a]/95 backdrop-blur border border-white/15 rounded-lg p-2 text-xs shadow-xl min-w-[220px] max-h-[360px] overflow-y-auto">
      <div className="text-white/70 mb-1 font-medium">{label}</div>
      <div className="space-y-0.5">
        {entries.map(e => (
          <div key={e.name} className="flex justify-between gap-3 items-center">
            <span className="flex items-center gap-1.5 text-white/85 truncate">
              <span className="w-2 h-2 rounded-full shrink-0" style={{ backgroundColor: e.color }} />
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
        <div className="flex justify-between mt-1.5 pt-1.5 border-t border-white/10 sticky bottom-0 bg-[#0a0a0a]/95">
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
  const [metric, setMetric] = useState<Metric>('tvl');
  const [view, setView] = useState<View>('platform');
  const [range, setRange] = useState<Range>('all');
  const [showTges, setShowTges] = useState<boolean>(true);
  const [hidden, setHidden] = useState<Set<string>>(new Set());

  useEffect(() => {
    fetch('/tvl.json').then(r => r.json()).then(setTvl).catch(() => null);
    fetch('/volume.json').then(r => r.json()).then(setVol).catch(() => null);
  }, []);

  const effectiveView = metric === 'breakdown' ? 'protocol' : view;
  const dates = metric === 'volume' ? vol?.dates : tvl?.dates;
  const start = dates ? rangeStart(dates, range) : 0;

  const { allKeys, data, colorMap, isFlat, breakdownLike } = useMemo(() => {
    type Result = {
      allKeys: string[]; data: any[]; colorMap: Record<string, string>;
      isFlat: boolean; breakdownLike: boolean;
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

    function emitFromSeries(
      seriesByKey: Record<string, number[]>,
      sortBy: 'latest' | 'sum',
      isMarket: boolean,
      platformResolver: (k: string) => string,
    ) {
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
  }, [tvl, vol, metric, effectiveView, range, start, dates]);

  // Reset hidden set when keys change. Show top-DEFAULT_TOP visible; for
  // platform view (typically <15 entries) show all by default.
  useEffect(() => {
    if (isFlat || breakdownLike) { setHidden(new Set()); return; }
    if (effectiveView === 'platform' || allKeys.length <= DEFAULT_TOP) {
      setHidden(new Set()); return;
    }
    setHidden(new Set(allKeys.slice(DEFAULT_TOP)));
  }, [allKeys, isFlat, breakdownLike, effectiveView]);

  const visibleKeys = useMemo(() => allKeys.filter(k => !hidden.has(k)), [allKeys, hidden]);

  // TGE markers in visible range
  const tgesVisible = useMemo(() => {
    if (!tvl?.tgeMarkers || !data.length) return [];
    const firstDate = data[0]?.date as string;
    const lastDate = data[data.length - 1]?.date as string;
    return tvl.tgeMarkers.filter(t => t.date >= firstDate && t.date <= lastDate);
  }, [tvl, data]);

  function toggle(k: string) {
    setHidden(prev => { const n = new Set(prev); if (n.has(k)) n.delete(k); else n.add(k); return n; });
  }
  const hideAll = () => setHidden(new Set(allKeys));
  const showAll = () => setHidden(new Set());
  const isToggleable = !isFlat && !breakdownLike;

  if (!tvl || !vol) return <div className="text-white/40 text-sm p-4">Loading chart…</div>;

  // X-axis label thinning — render at most ~8 month labels
  const interval = Math.max(0, Math.floor(data.length / 8));

  return (
    <section className="rounded-2xl border border-white/10 bg-white/5 p-4 mb-6">
      <header className="flex items-center justify-between flex-wrap gap-3 mb-3">
        <div className="flex items-center gap-1 flex-wrap">
          {/* Metric tabs — match v1 order */}
          {([
            ['tvl', 'TVL'],
            ['volume', 'Volume'],
            ['positions', 'Positions'],
            ['breakdown', 'Breakdown'],
          ] as [Metric, string][]).map(([m, label]) => (
            <button key={m} onClick={() => setMetric(m)}
              className={`text-xs px-3 py-1.5 rounded-lg border transition ${metric === m ? 'border-white/30 bg-white/10 text-white' : 'border-white/10 text-white/40 hover:text-white'}`}>
              {label}
            </button>
          ))}
          <span className="w-3" />
          {(['protocol', 'platform', 'market'] as View[]).map(v => {
            const disabled = metric === 'breakdown' && v !== 'protocol';
            return (
              <button key={v} onClick={() => !disabled && setView(v)} disabled={disabled}
                className={`text-xs px-3 py-1 rounded-lg transition ${
                  disabled ? 'text-white/15 cursor-not-allowed'
                  : effectiveView === v ? 'bg-white/10 text-white'
                  : 'text-white/40 hover:text-white'
                }`}>
                {v.charAt(0).toUpperCase() + v.slice(1)}
              </button>
            );
          })}
        </div>
        <div className="flex items-center gap-1">
          <button onClick={() => setShowTges(v => !v)}
            className={`text-[11px] px-2.5 py-1 rounded-md border transition ${
              showTges ? 'border-amber-400/40 bg-amber-400/10 text-amber-300'
                       : 'border-white/10 text-white/30 hover:text-white/60'
            }`}>
            TGEs
          </button>
          {(['30d', '90d', '1y', 'all'] as Range[]).map(r => (
            <button key={r} onClick={() => setRange(r)}
              className={`text-xs px-2.5 py-1 rounded-md transition ${range === r ? 'bg-white/10 text-white' : 'text-white/30 hover:text-white/60'}`}>
              {r === 'all' ? 'All' : r.toUpperCase()}
            </button>
          ))}
        </div>
      </header>

      <ResponsiveContainer width="100%" height={380}>
        <BarChart data={data} margin={{ top: 20, right: 16, left: 8, bottom: 4 }}>
          <XAxis dataKey="date" tick={{ fill: '#888', fontSize: 11 }} tickFormatter={fmtMonth}
                 axisLine={false} tickLine={false} interval={interval} />
          <YAxis tick={{ fill: '#888', fontSize: 11 }} tickFormatter={fmtUsd}
                 axisLine={false} tickLine={false} width={70} />
          <Tooltip content={<SortedTooltip />} cursor={{ fill: 'rgba(255,255,255,0.04)' }} />
          {visibleKeys.map(k => (
            <Bar key={k} dataKey={k} stackId="s"
                 fill={colorMap[k] ?? '#9ca3af'} fillOpacity={0.9}
                 isAnimationActive={false} />
          ))}
          {showTges && tgesVisible.map(t => (
            <ReferenceLine key={t.platform + t.date} x={t.date}
              stroke="#fbbf24" strokeDasharray="3 3" strokeOpacity={0.55}
              label={{ value: t.platform, position: 'top', fill: '#fbbf24', fontSize: 10, opacity: 0.85 }} />
          ))}
        </BarChart>
      </ResponsiveContainer>

      {/* v1-style legend: Hide All / Show All + colored dots + labels */}
      {isToggleable && allKeys.length > 0 && (
        <div className="mt-3 flex items-center justify-center gap-x-4 gap-y-2 flex-wrap">
          <button onClick={hidden.size === allKeys.length ? showAll : hideAll}
            className="text-[11px] px-2 py-0.5 rounded border border-white/15 text-white/60 hover:text-white hover:bg-white/5">
            {hidden.size === allKeys.length ? 'Show All' : 'Hide All'}
          </button>
          {allKeys.map(k => {
            const isHidden = hidden.has(k);
            return (
              <button key={k} onClick={() => toggle(k)}
                className={`flex items-center gap-1.5 text-[11px] transition ${
                  isHidden ? 'text-white/30' : 'text-white/80 hover:text-white'
                }`}>
                <span className="inline-block w-2.5 h-2.5 rounded-full"
                      style={{ backgroundColor: colorMap[k], opacity: isHidden ? 0.3 : 1 }} />
                <span className={isHidden ? 'line-through' : ''}>{k}</span>
              </button>
            );
          })}
        </div>
      )}
    </section>
  );
}
