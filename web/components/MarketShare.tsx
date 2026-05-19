'use client';
import { useEffect, useMemo, useState } from 'react';
import { BarChart, Bar, XAxis, YAxis, Tooltip, ResponsiveContainer, TooltipProps } from 'recharts';
import { colorForTicker, platformOfTicker, colorForMarket } from '@/lib/colors';

type SnapshotRow = {
  ticker: string;
  volumeUsd: number;
  volumeSharePct: number;
  tvlUsd: number;
  tvlSharePct: number;
};
type RollingRow = {
  ticker: string;
  volumeUsd30d: number;
  volumeShare30dPct: number;
  tvlUsdAvg30d: number;
  tvlShare30dPct: number;
};
type ByTicker = Record<string, { volumeUsd: number[]; tvlUsd: number[] }>;
type ShareData = {
  meta: { generatedAt: string; dateRange: [string, string]; tickers: string[] };
  dates: string[];
  byTicker: ByTicker;
  snapshot: SnapshotRow[];
  rolling30d: RollingRow[];
};

type Metric = 'volume' | 'tvl';
type Mode = 'abs' | 'pct';
type Range = '30d' | '90d' | '1y' | 'all';

const TOP_N = 10;

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
    <div className="bg-[#0a0a0a] border border-white/15 rounded-lg p-2 text-xs shadow-xl min-w-[220px]">
      <div className="text-white/70 mb-1 font-medium">{label}</div>
      <div className="space-y-0.5 max-h-[280px] overflow-y-auto">
        {entries.map(e => (
          <div key={e.name} className="flex justify-between gap-3 items-center">
            <span className="flex items-center gap-1.5 text-white/85 truncate">
              <span className="w-2 h-2 rounded-sm" style={{ backgroundColor: e.color }} />
              <span className="truncate">{e.name}</span>
            </span>
            <span className="tabular-nums text-white">
              {mode === 'pct' ? `${e.value.toFixed(1)}%` : fmtUsd(e.value)}
              {mode === 'abs' && total > 0 && (
                <span className="text-white/40 ml-1.5">({((e.value/total)*100).toFixed(0)}%)</span>
              )}
            </span>
          </div>
        ))}
      </div>
      {mode === 'abs' && (
        <div className="flex justify-between mt-1.5 pt-1.5 border-t border-white/10">
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

  useEffect(() => {
    fetch('/market_share.json')
      .then(r => (r.ok ? r.json() : Promise.reject(`HTTP ${r.status}`)))
      .then(setData)
      .catch(e => setErr(String(e)));
  }, []);

  // Pick the top-N tickers by lifetime volume/TVL within the visible range
  const { topTickers, chartData } = useMemo(() => {
    if (!data) return { topTickers: [] as string[], chartData: [] as any[] };
    const start = rangeStart(data.dates, range);
    const sliced = data.dates.slice(start);
    const key = metric === 'volume' ? 'volumeUsd' : 'tvlUsd';

    // Compute per-ticker total over the visible window
    const totals = Object.entries(data.byTicker).map(([ticker, series]) => ({
      ticker,
      total: series[key].slice(start).reduce((s, v) => s + (v || 0), 0),
    }));
    totals.sort((a, b) => b.total - a.total);
    const top = totals.slice(0, TOP_N).filter(t => t.total > 0).map(t => t.ticker);
    const otherTickers = totals.slice(TOP_N).map(t => t.ticker);

    // Build rows: one per date with each top ticker + Other
    const rows = sliced.map((d, i) => {
      const idx = start + i;
      const row: Record<string, number | string> = { date: d };
      let dayTotal = 0;
      for (const tk of top) {
        const v = data.byTicker[tk][key][idx] || 0;
        row[tk] = v;
        dayTotal += v;
      }
      let other = 0;
      for (const tk of otherTickers) other += data.byTicker[tk][key][idx] || 0;
      row['Other'] = other;
      dayTotal += other;

      if (mode === 'pct' && dayTotal > 0) {
        for (const tk of top) row[tk] = (Number(row[tk]) / dayTotal) * 100;
        row['Other'] = (other / dayTotal) * 100;
      }
      return row;
    });

    return { topTickers: [...top, 'Other'], chartData: rows };
  }, [data, metric, mode, range]);

  // Color per ticker: same hue family for tickers on the same platform,
  // shaded by index within that family so siblings differ visibly.
  const tickerColor = useMemo(() => {
    const seen: Record<string, number> = {};
    const out: Record<string, string> = {};
    for (const tk of topTickers) {
      if (tk === 'Other') { out[tk] = '#9ca3af'; continue; }
      const p = platformOfTicker(tk);
      const i = seen[p] || 0;
      out[tk] = colorForMarket(p, i);
      seen[p] = i + 1;
    }
    return out;
  }, [topTickers]);

  // Dominance: which ticker leads the most recent day
  const leader = useMemo(() => {
    if (!data || !chartData.length) return null;
    const last = chartData[chartData.length - 1];
    let best = '', bestVal = 0;
    for (const tk of topTickers) {
      const v = Number(last[tk] ?? 0);
      if (v > bestVal) { bestVal = v; best = tk; }
    }
    return { ticker: best, value: bestVal };
  }, [data, chartData, topTickers]);

  if (err) return <div className="text-red-400 text-sm p-4">Failed to load market_share.json: {err}</div>;
  if (!data) return <div className="text-white/40 text-sm p-4">Loading market share…</div>;

  return (
    <section className="rounded-2xl border border-white/10 bg-white/5 p-4 mb-6">
      <header className="flex items-baseline justify-between flex-wrap gap-2 mb-3">
        <div>
          <h2 className="text-sm uppercase tracking-wider text-white/60">Market Share</h2>
          <p className="text-xs text-white/40">
            Top {TOP_N} tickers {metric === 'volume' ? 'by volume' : 'by TVL'} • {mode === 'pct' ? 'share %' : 'absolute USD'}
            {leader && leader.ticker && (
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
          <Tooltip content={<StackedTooltip mode={mode} />} />
          {topTickers.map(tk => (
            <Bar key={tk} dataKey={tk} stackId="s"
                 fill={tickerColor[tk]} fillOpacity={0.9} />
          ))}
        </BarChart>
      </ResponsiveContainer>

      {/* Legend */}
      <div className="flex flex-wrap gap-3 mt-3 text-[11px] text-white/70 justify-center">
        {topTickers.map(tk => (
          <span key={tk} className="flex items-center gap-1.5">
            <span className="w-2 h-2 rounded-sm" style={{ backgroundColor: tickerColor[tk] }} />
            {tk}
          </span>
        ))}
      </div>
    </section>
  );
}
