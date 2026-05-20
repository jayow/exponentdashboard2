'use client';
import { useEffect, useMemo, useState } from 'react';
import { BarChart, Bar, XAxis, YAxis, Tooltip, ResponsiveContainer, TooltipProps } from 'recharts';
import { platformOfTicker, colorForMarket, colorForPlatform } from '@/lib/colors';

type ByTicker = Record<string, { volumeUsd: number[]; tvlUsd: number[] }>;
type ShareData = {
  meta: { generatedAt: string; dateRange: [string, string]; tickers: string[] };
  dates: string[];
  byTicker: ByTicker;
};
type VolData = {
  dates: string[];
  byPlatform: Record<string, number[]>;
  byMarket: Record<string, { ticker: string; totalUsd: number[] }>;
};
type TvlData = {
  dates: string[];
  byPlatform: Record<string, number[]>;
  byMarket: Record<string, { ticker: string; tvlUsd: number[] }>;
};

type Metric = 'volume' | 'tvl';
type View = 'ticker' | 'platform' | 'market';
type Range = '30d' | '90d' | '1y' | 'all';

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

function StackedTooltip({ active, payload, label }: TooltipProps<number, string>) {
  if (!active || !payload?.length) return null;
  const entries = payload
    .map(p => ({ name: String(p.name ?? p.dataKey ?? ''), value: typeof p.value === 'number' ? p.value : 0, color: p.color || (p as any).fill || '#888' }))
    .filter(e => e.value > 0.01)
    .sort((a, b) => b.value - a.value);
  if (!entries.length) return null;
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
              {e.value.toFixed(1)}%
            </span>
          </div>
        ))}
      </div>
    </div>
  );
}

export function MarketShare() {
  const [data, setData] = useState<ShareData | null>(null);
  const [vol, setVol] = useState<VolData | null>(null);
  const [tvl, setTvl] = useState<TvlData | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const [metric, setMetric] = useState<Metric>('volume');
  const [view, setView] = useState<View>('ticker');
  const [range, setRange] = useState<Range>('90d');
  const [hidden, setHidden] = useState<Set<string>>(new Set());

  useEffect(() => {
    Promise.all([
      fetch('/market_share.json').then(r => (r.ok ? r.json() : Promise.reject(`HTTP ${r.status}`))),
      fetch('/volume.json').then(r => r.json()),
      fetch('/tvl.json').then(r => r.json()),
    ])
      .then(([m, v, t]) => { setData(m); setVol(v); setTvl(t); })
      .catch(e => setErr(String(e)));
  }, []);

  // Aggregate-by-view source. Each view exposes a `dates` axis and a
  // {key → number[]} series map. Range slicing happens later in chartData.
  const { dates, perKeySeries } = useMemo(() => {
    if (view === 'ticker') {
      if (!data) return { dates: [] as string[], perKeySeries: {} as Record<string, number[]> };
      const key = metric === 'volume' ? 'volumeUsd' : 'tvlUsd';
      const m: Record<string, number[]> = {};
      for (const [tk, s] of Object.entries(data.byTicker)) m[tk] = s[key];
      return { dates: data.dates, perKeySeries: m };
    }
    if (view === 'platform') {
      const src = metric === 'volume' ? vol : tvl;
      if (!src) return { dates: [], perKeySeries: {} };
      return { dates: src.dates, perKeySeries: { ...src.byPlatform } };
    }
    // market view
    const src = metric === 'volume' ? vol : tvl;
    if (!src) return { dates: [], perKeySeries: {} };
    const m: Record<string, number[]> = {};
    for (const [mk, e] of Object.entries(src.byMarket)) {
      if (mk.includes('(unsplit)')) continue;
      m[mk] = (e as any)[metric === 'volume' ? 'totalUsd' : 'tvlUsd'];
    }
    return { dates: src.dates, perKeySeries: m };
  }, [view, metric, data, vol, tvl]);

  // All keys sorted by lifetime contribution within the visible window.
  const allKeys = useMemo(() => {
    if (!dates.length) return [] as string[];
    const start = rangeStart(dates, range);
    return Object.entries(perKeySeries)
      .map(([k, s]) => ({ k, total: s.slice(start).reduce((a, v) => a + (v || 0), 0) }))
      .filter(x => x.total > 0)
      .sort((a, b) => b.total - a.total)
      .map(x => x.k);
  }, [perKeySeries, dates, range]);

  // Reset hidden when the key set changes.
  useEffect(() => { setHidden(new Set()); }, [allKeys]);

  const visibleKeys = useMemo(() => allKeys.filter(k => !hidden.has(k)), [allKeys, hidden]);

  // Build chart rows: one per date with each visible key as a % of visible total.
  const chartData = useMemo(() => {
    if (!dates.length) return [];
    const start = rangeStart(dates, range);
    const sliced = dates.slice(start);
    return sliced.map((d, i) => {
      const idx = start + i;
      const row: Record<string, number | string> = { date: d };
      let visibleTotal = 0;
      for (const k of visibleKeys) {
        const v = perKeySeries[k]?.[idx] || 0;
        row[k] = v;
        visibleTotal += v;
      }
      if (visibleTotal > 0) {
        for (const k of visibleKeys) row[k] = (Number(row[k]) / visibleTotal) * 100;
      }
      return row;
    });
  }, [dates, visibleKeys, perKeySeries, range]);

  // Per-key color. View determines the coloring scheme.
  const keyColor = useMemo(() => {
    const out: Record<string, string> = {};
    if (view === 'platform') {
      for (const k of allKeys) out[k] = colorForPlatform(k);
    } else {
      // ticker or market — group by platform with sibling shading
      const seen: Record<string, number> = {};
      for (const k of allKeys) {
        const tk = view === 'market' ? k.split('-')[0] : k;
        const p = platformOfTicker(tk);
        const i = seen[p] || 0;
        out[k] = colorForMarket(p, i);
        seen[p] = i + 1;
      }
    }
    return out;
  }, [allKeys, view]);

  // Latest-day leader (among visible)
  const leader = useMemo(() => {
    if (!chartData.length) return null;
    const last = chartData[chartData.length - 1];
    let best = '', bestVal = 0;
    for (const k of visibleKeys) {
      const v = Number(last[k] ?? 0);
      if (v > bestVal) { bestVal = v; best = k; }
    }
    return best ? { key: best, value: bestVal } : null;
  }, [chartData, visibleKeys]);

  function toggle(k: string) {
    setHidden(prev => { const n = new Set(prev); if (n.has(k)) n.delete(k); else n.add(k); return n; });
  }
  const showAll  = () => setHidden(new Set());
  const showNone = () => setHidden(new Set(allKeys));

  if (err) return <div className="text-red-400 text-sm p-4">Failed to load market_share.json: {err}</div>;
  if (!data || !vol || !tvl) return <div className="text-white/40 text-sm p-4">Loading market share…</div>;

  return (
    <section className="rounded-2xl border border-white/10 bg-white/5 p-4 mb-6">
      <header className="flex items-baseline justify-between flex-wrap gap-2 mb-3">
        <div>
          <h2 className="text-sm uppercase tracking-wider text-white/60">Market Share</h2>
          <p className="text-xs text-white/40">
            {visibleKeys.length} of {allKeys.length} {view === 'platform' ? 'platforms' : view === 'market' ? 'markets' : 'tickers'} • {metric === 'volume' ? 'volume' : 'TVL'} share %
            {leader && (
              <span className="text-white/30"> • latest leader: {leader.key} ({leader.value.toFixed(0)}%)</span>
            )}
          </p>
        </div>
        <div className="flex items-center gap-1 flex-wrap">
          {(['volume', 'tvl'] as Metric[]).map(m => (
            <button key={m} onClick={() => setMetric(m)}
              className={`text-xs px-3 py-1 rounded-lg border ${metric === m ? 'border-white/30 bg-white/10' : 'border-white/10 text-white/40'}`}>
              {m === 'volume' ? 'Volume' : 'TVL'}
            </button>
          ))}
          <span className="w-2" />
          {(['ticker', 'platform', 'market'] as View[]).map(v => (
            <button key={v} onClick={() => setView(v)}
              className={`text-xs px-3 py-1 rounded-lg transition ${
                view === v ? 'bg-white/10 text-white' : 'text-white/40 hover:text-white'
              }`}>
              {v.charAt(0).toUpperCase() + v.slice(1)}
            </button>
          ))}
          <span className="w-2" />
          {(['30d', '90d', '1y', 'all'] as Range[]).map(r => (
            <button key={r} onClick={() => setRange(r)}
              className={`text-xs px-2.5 py-1 rounded-md ${range === r ? 'bg-white/10 text-white' : 'text-white/30 hover:text-white/60'}`}>
              {r === 'all' ? 'All' : r.toUpperCase()}
            </button>
          ))}
        </div>
      </header>

      <ResponsiveContainer width="100%" height={320}>
        <BarChart data={chartData} margin={{ top: 10, right: 10, left: 0, bottom: 0 }}>
          <XAxis dataKey="date" tick={{ fill: '#888', fontSize: 11 }} axisLine={false} tickLine={false} />
          <YAxis
            tick={{ fill: '#888', fontSize: 11 }} axisLine={false} tickLine={false}
            tickFormatter={v => `${v.toFixed(0)}%`}
            domain={[0, 100]}
          />
          <Tooltip content={<StackedTooltip />} cursor={{ fill: 'rgba(255,255,255,0.04)' }} />
          {visibleKeys.map(k => (
            <Bar key={k} dataKey={k} stackId="s"
                 fill={keyColor[k]} fillOpacity={0.9} />
          ))}
        </BarChart>
      </ResponsiveContainer>

      {/* Interactive legend */}
      <div className="mt-3 border-t border-white/10 pt-3">
        <div className="flex items-center justify-between mb-2 flex-wrap gap-2">
          <div className="text-[11px] text-white/40">
            {visibleKeys.length} of {allKeys.length} visible
            <span className="text-white/30"> · click to toggle</span>
          </div>
          <div className="flex items-center gap-1">
            <button onClick={hidden.size === allKeys.length ? showAll : showNone}
              className="text-[11px] px-2 py-0.5 rounded border border-white/15 text-white/60 hover:text-white hover:bg-white/5">
              {hidden.size === allKeys.length ? 'Show All' : 'Hide All'}
            </button>
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
                      style={{ backgroundColor: keyColor[k], opacity: isHidden ? 0.3 : 1 }} />
                <span className={isHidden ? 'line-through' : ''}>{k}</span>
              </button>
            );
          })}
        </div>
      </div>
    </section>
  );
}
