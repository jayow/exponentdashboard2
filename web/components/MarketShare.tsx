'use client';
import { useEffect, useMemo, useState } from 'react';
import { BarChart, Bar, XAxis, YAxis, Tooltip, ResponsiveContainer, TooltipProps } from 'recharts';
import { platformOfTicker, colorForMarket } from '@/lib/colors';

type ByTicker = Record<string, { volumeUsd: number[]; tvlUsd: number[] }>;
type ShareData = {
  meta: { generatedAt: string; dateRange: [string, string]; tickers: string[] };
  dates: string[];
  byTicker: ByTicker;
};

type Metric = 'volume' | 'tvl';
type Mode = 'abs' | 'pct';
type Range = '30d' | '90d' | '1y' | 'all';

const DEFAULT_TOP = 10;

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

function StackedTooltip({ active, payload, label, mode }: TooltipProps<number, string> & { mode: Mode }) {
  if (!active || !payload?.length) return null;
  const entries = payload
    .map(p => ({ name: String(p.name ?? p.dataKey ?? ''), value: typeof p.value === 'number' ? p.value : 0, color: p.color || (p as any).fill || '#888' }))
    .filter(e => e.value > 0.01)
    .sort((a, b) => b.value - a.value);
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
              {mode === 'pct' ? `${e.value.toFixed(1)}%` : fmtUsd(e.value)}
              {mode === 'abs' && total > 0 && (
                <span className="text-white/40 ml-1.5">({((e.value/total)*100).toFixed(0)}%)</span>
              )}
            </span>
          </div>
        ))}
      </div>
      {mode === 'abs' && (
        <div className="flex justify-between mt-1.5 pt-1.5 border-t border-white/10 sticky bottom-0 bg-[#0a0a0a]">
          <span className="text-white/40">Total</span>
          <span className="tabular-nums text-white/90 font-medium">{fmtUsd(total)}</span>
        </div>
      )}
    </div>
  );
}

export function MarketShare() {
  const [data, setData] = useState<ShareData | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const [metric, setMetric] = useState<Metric>('volume');
  const [mode, setMode] = useState<Mode>('pct');
  const [range, setRange] = useState<Range>('90d');
  const [hidden, setHidden] = useState<Set<string>>(new Set());

  useEffect(() => {
    fetch('/market_share.json')
      .then(r => (r.ok ? r.json() : Promise.reject(`HTTP ${r.status}`)))
      .then(setData)
      .catch(e => setErr(String(e)));
  }, []);

  // All tickers sorted by lifetime contribution within the visible window.
  // No "Other" bucket — every ticker is its own bar segment.
  const { allTickers, perTickerSeries } = useMemo(() => {
    if (!data) return { allTickers: [] as string[], perTickerSeries: {} as Record<string, number[]> };
    const start = rangeStart(data.dates, range);
    const key = metric === 'volume' ? 'volumeUsd' : 'tvlUsd';
    const totals = Object.entries(data.byTicker).map(([ticker, series]) => ({
      ticker,
      total: series[key].slice(start).reduce((s, v) => s + (v || 0), 0),
      series: series[key],
    }));
    totals.sort((a, b) => b.total - a.total);
    const filtered = totals.filter(t => t.total > 0);
    return {
      allTickers: filtered.map(t => t.ticker),
      perTickerSeries: Object.fromEntries(filtered.map(t => [t.ticker, t.series])),
    };
  }, [data, metric, range]);

  // When metric/range changes, reset hidden to top-N default
  useEffect(() => {
    if (allTickers.length <= DEFAULT_TOP) { setHidden(new Set()); return; }
    setHidden(new Set(allTickers.slice(DEFAULT_TOP)));
  }, [allTickers]);

  const visibleTickers = useMemo(() => allTickers.filter(tk => !hidden.has(tk)), [allTickers, hidden]);

  // Build chart rows: one per date with each visible ticker (in pct or abs).
  // In pct mode, divide by sum-of-visible-tickers so the stack still sums
  // to ~100% even when some are toggled off.
  const chartData = useMemo(() => {
    if (!data) return [];
    const start = rangeStart(data.dates, range);
    const sliced = data.dates.slice(start);
    return sliced.map((d, i) => {
      const idx = start + i;
      const row: Record<string, number | string> = { date: d };
      let visibleTotal = 0;
      for (const tk of visibleTickers) {
        const v = perTickerSeries[tk]?.[idx] || 0;
        row[tk] = v;
        visibleTotal += v;
      }
      if (mode === 'pct' && visibleTotal > 0) {
        for (const tk of visibleTickers) row[tk] = (Number(row[tk]) / visibleTotal) * 100;
      }
      return row;
    });
  }, [data, visibleTickers, perTickerSeries, mode, range]);

  // Per-ticker color: same hue family for tickers on the same platform,
  // index-shaded so siblings differ within the family.
  const tickerColor = useMemo(() => {
    const seen: Record<string, number> = {};
    const out: Record<string, string> = {};
    for (const tk of allTickers) {
      const p = platformOfTicker(tk);
      const i = seen[p] || 0;
      out[tk] = colorForMarket(p, i);
      seen[p] = i + 1;
    }
    return out;
  }, [allTickers]);

  // Latest-day leader (among visible)
  const leader = useMemo(() => {
    if (!data || !chartData.length) return null;
    const last = chartData[chartData.length - 1];
    let best = '', bestVal = 0;
    for (const tk of visibleTickers) {
      const v = Number(last[tk] ?? 0);
      if (v > bestVal) { bestVal = v; best = tk; }
    }
    return best ? { ticker: best, value: bestVal } : null;
  }, [data, chartData, visibleTickers]);

  function toggle(tk: string) {
    setHidden(prev => { const n = new Set(prev); if (n.has(tk)) n.delete(tk); else n.add(tk); return n; });
  }
  const showAll  = () => setHidden(new Set());
  const showNone = () => setHidden(new Set(allTickers));
  const showTop  = (n: number) => setHidden(new Set(allTickers.slice(n)));

  if (err) return <div className="text-red-400 text-sm p-4">Failed to load market_share.json: {err}</div>;
  if (!data) return <div className="text-white/40 text-sm p-4">Loading market share…</div>;

  return (
    <section className="rounded-2xl border border-white/10 bg-white/5 p-4 mb-6">
      <header className="flex items-baseline justify-between flex-wrap gap-2 mb-3">
        <div>
          <h2 className="text-sm uppercase tracking-wider text-white/60">Market Share</h2>
          <p className="text-xs text-white/40">
            {visibleTickers.length} of {allTickers.length} tickers • {metric === 'volume' ? 'volume' : 'TVL'} • {mode === 'pct' ? 'share %' : 'absolute USD'}
            {leader && (
              <span className="text-white/30"> • latest leader: {leader.ticker} {mode === 'pct' ? `(${leader.value.toFixed(0)}%)` : `(${fmtUsd(leader.value)})`}</span>
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
          {(['pct', 'abs'] as Mode[]).map(m => (
            <button key={m} onClick={() => setMode(m)}
              className={`text-xs px-3 py-1 rounded-lg border ${mode === m ? 'border-white/30 bg-white/10' : 'border-white/10 text-white/40'}`}>
              {m === 'pct' ? 'Share %' : 'Absolute'}
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
            tickFormatter={mode === 'pct' ? (v => `${v.toFixed(0)}%`) : fmtUsd}
            domain={mode === 'pct' ? [0, 100] : ['auto', 'auto']}
          />
          <Tooltip content={<StackedTooltip mode={mode} />} cursor={{ fill: 'rgba(255,255,255,0.04)' }} />
          {visibleTickers.map(tk => (
            <Bar key={tk} dataKey={tk} stackId="s"
                 fill={tickerColor[tk]} fillOpacity={0.9} />
          ))}
        </BarChart>
      </ResponsiveContainer>

      {/* Interactive legend */}
      <div className="mt-3 border-t border-white/10 pt-3">
        <div className="flex items-center justify-between mb-2 flex-wrap gap-2">
          <div className="text-[11px] text-white/40">
            {visibleTickers.length} of {allTickers.length} visible
            {allTickers.length > DEFAULT_TOP && <span className="text-white/30"> · click to toggle</span>}
          </div>
          <div className="flex items-center gap-1">
            <button onClick={showAll}
              className={`text-[11px] px-2 py-0.5 rounded border ${hidden.size === 0 ? 'border-white/30 bg-white/10 text-white' : 'border-white/10 text-white/40 hover:text-white'}`}>
              All
            </button>
            <button onClick={showNone}
              className={`text-[11px] px-2 py-0.5 rounded border ${hidden.size === allTickers.length ? 'border-white/30 bg-white/10 text-white' : 'border-white/10 text-white/40 hover:text-white'}`}>
              None
            </button>
            {allTickers.length > 10 && (
              <button onClick={() => showTop(10)}
                className="text-[11px] px-2 py-0.5 rounded border border-white/10 text-white/40 hover:text-white">
                Top 10
              </button>
            )}
            {allTickers.length > 15 && (
              <button onClick={() => showTop(15)}
                className="text-[11px] px-2 py-0.5 rounded border border-white/10 text-white/40 hover:text-white">
                Top 15
              </button>
            )}
          </div>
        </div>
        <div className="flex flex-wrap gap-1.5 max-h-[110px] overflow-y-auto">
          {allTickers.map(tk => {
            const isHidden = hidden.has(tk);
            return (
              <button key={tk} onClick={() => toggle(tk)}
                className={`text-[11px] px-2 py-0.5 rounded border transition ${
                  isHidden
                    ? 'border-white/5 bg-transparent text-white/30'
                    : 'border-white/15 bg-white/5 text-white/85 hover:bg-white/10'
                }`}>
                <span className="inline-block w-2 h-2 rounded-sm mr-1 align-middle"
                      style={{ backgroundColor: tickerColor[tk], opacity: isHidden ? 0.3 : 1 }} />
                <span className={isHidden ? 'line-through' : ''}>{tk}</span>
              </button>
            );
          })}
        </div>
      </div>
    </section>
  );
}
