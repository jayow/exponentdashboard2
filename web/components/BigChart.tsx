'use client';
import { useEffect, useMemo, useState } from 'react';
import {
  BarChart, Bar, XAxis, YAxis, Tooltip, ResponsiveContainer, ReferenceLine, TooltipProps, Brush,
} from 'recharts';
import { colorForPlatform, colorForMarket, platformOfTicker } from '@/lib/colors';

type TgeMarker = { platform: string; date: string };
type TvlData = {
  dates: string[];
  protocolUsd: number[];
  protocolPrincipalUsd: number[];
  decomposition: { principalPt: number[]; farmYt: number[]; liquidityLp: number[]; idle: number[] };
  byPlatform: Record<string, number[]>;
  byPlatformBreakdown: Record<string, { principalPt: number[]; farmYt: number[]; liquidityLp: number[]; idle: number[] }>;
  byMarket: Record<string, {
    ticker: string; platform: string; tvlUsd: number[];
    ptUsd?: number[]; ytUsd?: number[]; lpUsd?: number[]; idleUsd?: number[];
  }>;
  tgeMarkers?: TgeMarker[];
};
type VolData = {
  dates: string[];
  protocol: { totalUsd: number[]; ptUsd: number[]; ytUsd: number[] };
  byPlatform: Record<string, number[]>;
  byMarket: Record<string, { ticker: string; totalUsd: number[]; ptUsd: number[]; ytUsd: number[] }>;
};

type Metric = 'tvl' | 'volume' | 'breakdown';
type View = 'protocol' | 'platform' | 'market';
type Range = '30d' | '90d' | '1y' | 'all';

const BREAKDOWN_COLOR: Record<string, string> = {
  'Principal (PT)': '#a78bfa',
  'Farm (YT)':      '#fb923c',
  'Liquidity (LP)': '#4ade80',
  'Idle':           '#9ca3af',
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

const MONTH_ABBR_TO_NUM: Record<string, number> = {
  JAN: 0, FEB: 1, MAR: 2, APR: 3, MAY: 4, JUN: 5,
  JUL: 6, AUG: 7, SEP: 8, OCT: 9, NOV: 10, DEC: 11,
};
// Parse maturity from a market_key like 'fragSOL-15DEC26' → ms epoch.
// Returns null for keys without a trailing date suffix (UNCLASSIFIED, etc.).
function maturityMs(marketKey: string): number | null {
  const m = marketKey.match(/-(\d{2})([A-Z]{3})(\d{2})$/);
  if (!m) return null;
  const month = MONTH_ABBR_TO_NUM[m[2]];
  if (month === undefined) return null;
  return Date.UTC(2000 + Number(m[3]), month, Number(m[1]));
}

const TOOLTIP_CAP = 15;  // show top N entries; sum the rest into a single row
function SortedTooltip({ active, payload, label }: TooltipProps<number, string>) {
  if (!active || !payload?.length) return null;
  const allEntries = payload
    .map(p => ({ name: String(p.name ?? p.dataKey ?? ''), value: typeof p.value === 'number' ? p.value : 0, color: p.color || (p as any).fill || '#888' }))
    .filter(e => Math.abs(e.value) > 0.01)
    .sort((a, b) => Math.abs(b.value) - Math.abs(a.value));
  if (!allEntries.length) return null;
  const total = allEntries.reduce((s, e) => s + e.value, 0);
  const top = allEntries.slice(0, TOOLTIP_CAP);
  const tail = allEntries.slice(TOOLTIP_CAP);
  const tailSum = tail.reduce((s, e) => s + e.value, 0);
  return (
    <div className="bg-[#0a0a0a]/95 backdrop-blur border border-white/15 rounded-lg p-2 text-xs shadow-xl min-w-[220px]">
      <div className="text-white/70 mb-1 font-medium">{label}</div>
      <div className="space-y-0.5">
        {top.map(e => (
          <div key={e.name} className="flex justify-between gap-3 items-center">
            <span className="flex items-center gap-1.5 text-white/85 truncate">
              <span className="w-2 h-2 rounded-full shrink-0" style={{ backgroundColor: e.color }} />
              <span className="truncate">{e.name}</span>
            </span>
            <span className="tabular-nums text-white shrink-0">
              {fmtUsd(e.value)}
              {allEntries.length > 1 && total !== 0 && <span className="text-white/40 ml-1.5">({((e.value/total)*100).toFixed(0)}%)</span>}
            </span>
          </div>
        ))}
        {tail.length > 0 && (
          <div className="flex justify-between gap-3 items-center text-white/40 italic">
            <span>… {tail.length} smaller</span>
            <span className="tabular-nums">{fmtUsd(tailSum)}</span>
          </div>
        )}
      </div>
      {allEntries.length > 1 && (
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
  const [metric, setMetric] = useState<Metric>('tvl');
  const [view, setView] = useState<View>('platform');
  const [range, setRange] = useState<Range>('all');
  const [showTges, setShowTges] = useState<boolean>(true);
  const [hidden, setHidden] = useState<Set<string>>(new Set());
  // Scope for Breakdown view: 'protocol' | 'platform:<name>' | 'market:<key>'
  const [breakdownScope, setBreakdownScope] = useState<string>('protocol');

  useEffect(() => {
    fetch('/tvl.json').then(r => r.json()).then(setTvl).catch(() => null);
    fetch('/volume.json').then(r => r.json()).then(setVol).catch(() => null);
  }, []);

  const effectiveView = metric === 'breakdown' ? 'protocol' : view;
  const dates = metric === 'volume' ? vol?.dates : tvl?.dates;
  const start = dates ? rangeStart(dates, range) : 0;
  const end = dates ? dates.length - 1 : 0;
  // Downsample only for the "All" range — 30d/90d/1y stay daily so the brush
  // doesn't need to flip granularity on drag (which used to make the handles
  // fight the data length).
  const stride = useMemo(() => {
    if (!dates) return 1;
    const visible = end - start + 1;
    if (visible <= 400) return 1;
    return Math.ceil(visible / 400);
  }, [dates, start, end]);

  const { allKeys, data, colorMap, isFlat, breakdownLike } = useMemo(() => {
    type Result = {
      allKeys: string[]; data: any[]; colorMap: Record<string, string>;
      isFlat: boolean; breakdownLike: boolean;
    };
    const empty: Result = { allKeys: [], data: [], colorMap: {}, isFlat: true, breakdownLike: false };
    if (!dates) return empty;
    // Strided iteration: take every Nth index in the visible window. Picks
    // indices 0, stride, 2·stride, … so the last point is always included.
    const indices: number[] = [];
    for (let i = start; i <= end; i += stride) indices.push(i);
    if (indices[indices.length - 1] !== end) indices.push(end);
    const sliced = indices.map(i => dates[i]);
    // Build a value lookup that takes the AVERAGE across each stride window,
    // so downsampled bars represent the visible week (not a single day).
    const sampleSeries = (s: number[]): number[] =>
      indices.map((iStart, k) => {
        const iEnd = k + 1 < indices.length ? indices[k + 1] : iStart + 1;
        let sum = 0, n = 0;
        for (let j = iStart; j < iEnd; j++) { sum += s[j] || 0; n++; }
        return n > 0 ? sum / n : 0;
      });

    if (metric === 'breakdown' && tvl) {
      // Pick the scope's 4-bucket source: protocol / platform:X / market:X
      let src: { principalPt: number[]; farmYt: number[]; liquidityLp: number[]; idle: number[] } | null = null;
      if (breakdownScope === 'protocol') {
        src = tvl.decomposition;
      } else if (breakdownScope.startsWith('platform:')) {
        const name = breakdownScope.slice('platform:'.length);
        src = tvl.byPlatformBreakdown?.[name] ?? null;
      } else if (breakdownScope.startsWith('market:')) {
        const mk = breakdownScope.slice('market:'.length);
        const m = tvl.byMarket?.[mk];
        if (m && m.ptUsd && m.ytUsd && m.lpUsd && m.idleUsd) {
          src = { principalPt: m.ptUsd, farmYt: m.ytUsd, liquidityLp: m.lpUsd, idle: m.idleUsd };
        }
      }
      if (!src) return empty;
      const pt = sampleSeries(src.principalPt);
      const yt = sampleSeries(src.farmYt);
      const lp = sampleSeries(src.liquidityLp);
      const idle = sampleSeries(src.idle);
      const keys = ['Principal (PT)', 'Farm (YT)', 'Liquidity (LP)', 'Idle'];
      return {
        allKeys: keys, isFlat: false, breakdownLike: true,
        colorMap: Object.fromEntries(keys.map(k => [k, BREAKDOWN_COLOR[k]])),
        data: sliced.map((dt, i) => ({
          date: dt,
          'Principal (PT)': pt[i] || 0,
          'Farm (YT)':      yt[i] || 0,
          'Liquidity (LP)': lp[i] || 0,
          'Idle':           idle[i] || 0,
        })),
      };
    }

    function emitFromSeries(
      seriesByKey: Record<string, number[]>,
      sortBy: 'latest' | 'sum' | 'maxInRange',
      isMarket: boolean,
      platformResolver: (k: string) => string,
    ) {
      // Compute sort value AND max-in-range together; sample down to the
      // visible-window stride once so we don't re-walk full daily series
      // later when building rows.
      type Entry = { k: string; sortVal: number; maxInRange: number; sampled: number[] };
      const entries: Entry[] = Object.entries(seriesByKey).map(([k, s]) => {
        let mx = 0, sum = 0;
        const last = Math.min(end, s.length - 1);
        for (let i = start; i <= last; i++) {
          const v = s[i] || 0;
          if (v > mx) mx = v;
          sum += v;
        }
        const latest = s[last] || 0;
        const sortVal = sortBy === 'latest' ? latest : sortBy === 'sum' ? sum : mx;
        return { k, sortVal, maxInRange: mx, sampled: sampleSeries(s) };
      }).filter(e => e.sortVal > 0).sort((a, b) => b.sortVal - a.sortVal);

      // Long-tail filter: drop series whose peak in the visible window is
      // <0.5% of the leader's peak. They render as invisible pixel slivers
      // but each costs a full Recharts <Area> SVG path. Tail gets lumped
      // into one "Other (small)" series so the stack still reconciles.
      // Active markets (maturity ≥ today) bypass the filter — small-but-live
      // markets belong in the legend even if they don't yet move the chart.
      const peakOfPeaks = entries[0]?.maxInRange ?? 0;
      const tailThreshold = peakOfPeaks * 0.005;
      const todayMs = Date.now();
      const isActive = (k: string) => {
        if (!isMarket) return false;
        const m = maturityMs(k);
        return m !== null && m >= todayMs;
      };
      const trimmed = entries.filter(e => e.maxInRange >= tailThreshold || isActive(e.k));
      const dropped = entries.filter(e => e.maxInRange < tailThreshold && !isActive(e.k));

      // Market-view legends read better in chronological order — newest
      // maturity first, so freshly-listed markets show at the head of the
      // chip row. Keys without a parseable maturity sink to the bottom.
      if (isMarket) {
        trimmed.sort((a, b) => {
          const ma = maturityMs(a.k);
          const mb = maturityMs(b.k);
          if (ma === null && mb === null) return 0;
          if (ma === null) return 1;
          if (mb === null) return -1;
          return mb - ma;
        });
      }

      const allKeys = trimmed.map(e => e.k);
      const rows = sliced.map((dt, i) => {
        const row: any = { date: dt };
        for (const e of trimmed) row[e.k] = e.sampled[i] || 0;
        return row;
      });
      if (dropped.length > 0) {
        const otherSeries: number[] = sliced.map((_, i) => {
          let s = 0;
          for (const e of dropped) s += e.sampled[i] || 0;
          return s;
        });
        if (otherSeries.some(v => v > 0)) {
          allKeys.push('Other (small)');
          for (let i = 0; i < rows.length; i++) rows[i]['Other (small)'] = otherSeries[i];
        }
      }

      const seen: Record<string, number> = {};
      const colorMap: Record<string, string> = {};
      for (const k of allKeys) {
        if (k === 'Other (small)') { colorMap[k] = '#9ca3af'; continue; }
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
        const sampled = sampleSeries(tvl.protocolUsd);
        return {
          allKeys: ['TVL'], isFlat: true, breakdownLike: false,
          colorMap: { TVL: BREAKDOWN_COLOR['TVL'] },
          data: sliced.map((dt, i) => ({ date: dt, TVL: sampled[i] || 0 })),
        };
      }
      if (effectiveView === 'platform') {
        return emitFromSeries(tvl.byPlatform, 'maxInRange', false, () => '');
      }
      const seriesByMk: Record<string, number[]> = {};
      const platformForMk: Record<string, string> = {};
      for (const [mk, m] of Object.entries(tvl.byMarket)) {
        seriesByMk[mk] = m.tvlUsd;
        platformForMk[mk] = m.platform || platformOfTicker(m.ticker);
      }
      return emitFromSeries(seriesByMk, 'maxInRange', true, (k: string) => platformForMk[k]);
    }

    if (metric === 'volume' && vol) {
      if (effectiveView === 'protocol') {
        const sampled = sampleSeries(vol.protocol.totalUsd);
        return {
          allKeys: ['Volume'], isFlat: true, breakdownLike: false,
          colorMap: { Volume: BREAKDOWN_COLOR['Volume'] },
          data: sliced.map((dt, i) => ({ date: dt, Volume: sampled[i] || 0 })),
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

    return empty;
  }, [tvl, vol, metric, effectiveView, range, start, end, stride, dates, breakdownScope]);

  // Default: show everything. User can toggle off individual series in
  // the legend (Hide All / per-chip click).
  useEffect(() => { setHidden(new Set()); }, [allKeys, isFlat, breakdownLike, effectiveView]);

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
            ['breakdown', 'Breakdown'],
          ] as [Metric, string][]).map(([m, label]) => (
            <button key={m} onClick={() => setMetric(m)}
              className={`text-xs px-3 py-1.5 rounded-lg border transition ${metric === m ? 'border-white/30 bg-white/10 text-white' : 'border-white/10 text-white/40 hover:text-white'}`}>
              {label}
            </button>
          ))}
          <span className="w-3" />
          {metric !== 'breakdown' ? (
            (['protocol', 'platform', 'market'] as View[]).map(v => (
              <button key={v} onClick={() => setView(v)}
                className={`text-xs px-3 py-1 rounded-lg transition ${
                  view === v ? 'bg-white/10 text-white' : 'text-white/40 hover:text-white'
                }`}>
                {v.charAt(0).toUpperCase() + v.slice(1)}
              </button>
            ))
          ) : (
            // Breakdown scope dropdown — protocol / each platform / each market
            <select
              value={breakdownScope}
              onChange={e => setBreakdownScope(e.target.value)}
              className="text-xs px-2 py-1 rounded-lg border border-white/15 bg-[#0a0a0a] text-white/85 hover:border-white/30 max-w-[260px]"
            >
              <option value="protocol">Protocol</option>
              <optgroup label="Platform">
                {tvl && Object.entries(tvl.byPlatform)
                  .sort((a, b) => (b[1][b[1].length - 1] || 0) - (a[1][a[1].length - 1] || 0))
                  .filter(([, s]) => (s[s.length - 1] || 0) > 100)
                  .map(([p]) => <option key={p} value={`platform:${p}`}>{p}</option>)}
              </optgroup>
              <optgroup label="Market">
                {tvl && Object.entries(tvl.byMarket)
                  .filter(([k, m]) => !k.includes('(unsplit)') && (m.tvlUsd[m.tvlUsd.length - 1] || 0) > 1000)
                  .sort((a, b) => (b[1].tvlUsd[b[1].tvlUsd.length - 1] || 0) - (a[1].tvlUsd[a[1].tvlUsd.length - 1] || 0))
                  .slice(0, 40)
                  .map(([k]) => <option key={k} value={`market:${k}`}>{k}</option>)}
              </optgroup>
            </select>
          )}
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
          {/* Date brush — narrow the visible window by dragging the handles.
              Uncontrolled: Recharts manages the zoom state internally so the
              handles track smoothly. Shown only on longer ranges. */}
          {(range === '1y' || range === 'all') && data.length > 2 && (
            <Brush
              dataKey="date"
              height={22}
              travellerWidth={8}
              stroke="#666"
              fill="rgba(255,255,255,0.04)"
              tickFormatter={fmtMonth}
            />
          )}
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
