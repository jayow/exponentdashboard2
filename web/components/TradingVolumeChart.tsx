'use client';
import { useEffect, useMemo, useState } from 'react';
import {
  ComposedChart, Bar, Line, XAxis, YAxis, Tooltip, ResponsiveContainer, Legend,
  TooltipProps,
} from 'recharts';

type Side = 'PT' | 'YT' | 'TOTAL';
type Range = '30d' | '90d' | '1y' | 'all';
type View = 'protocol' | 'topMarkets';
type Unit = 'usd' | 'underlying';

type VolumeData = {
  meta: {
    generatedAt: string;
    dateRange: [string, string];
    totalsUsd: { pt: number; yt: number; total: number };
    totalsUnderlying: { pt: number; yt: number; total: number };
    source: string;
    priceSources: string;
  };
  dates: string[];
  protocol: {
    ptUsd: number[]; ytUsd: number[]; totalUsd: number[];
    ptUnderlying: number[]; ytUnderlying: number[]; totalUnderlying: number[];
  };
  byMarket: Record<string, {
    ticker: string;
    ptUsd: number[]; ytUsd: number[]; totalUsd: number[];
    ptUnderlying: number[]; ytUnderlying: number[]; totalUnderlying: number[];
  }>;
  topMarkets: { marketKey: string; ticker: string; totalUsd: number }[];
};

// 20 distinct colors — kept high-contrast against #0a0a0a background
const COLORS = [
  '#38bdf8', '#a78bfa', '#4ade80', '#f87171', '#fbbf24',
  '#fb923c', '#22d3ee', '#e879f9', '#a3e635', '#818cf8',
  '#facc15', '#34d399', '#f472b6', '#60a5fa', '#fcd34d',
  '#c084fc', '#fb7185', '#86efac', '#7dd3fc', '#fda4af',
];

function fmtUsd(n: number) {
  if (Math.abs(n) >= 1e9) return `$${(n / 1e9).toFixed(2)}B`;
  if (Math.abs(n) >= 1e6) return `$${(n / 1e6).toFixed(2)}M`;
  if (Math.abs(n) >= 1e3) return `$${(n / 1e3).toFixed(1)}K`;
  return `$${n.toFixed(2)}`;
}
function fmtNum(n: number) {
  if (Math.abs(n) >= 1e9) return `${(n / 1e9).toFixed(2)}B`;
  if (Math.abs(n) >= 1e6) return `${(n / 1e6).toFixed(2)}M`;
  if (Math.abs(n) >= 1e3) return `${(n / 1e3).toFixed(1)}K`;
  return n.toFixed(2);
}

function rangeStartIdx(dates: string[], range: Range): number {
  if (range === 'all') return 0;
  const today = new Date(dates[dates.length - 1]);
  const days = range === '30d' ? 30 : range === '90d' ? 90 : 365;
  const cutoff = new Date(today.getTime() - days * 86400_000);
  const cutoffStr = cutoff.toISOString().slice(0, 10);
  return dates.findIndex(d => d >= cutoffStr);
}

/** Sort tooltip entries by absolute value descending, drop zero rows. */
function SortedTooltip(
  { active, payload, label, fmt }: TooltipProps<number, string> & { fmt: (n: number) => string }
) {
  if (!active || !payload?.length) return null;
  const entries = payload
    .map(p => ({
      name: String(p.name ?? p.dataKey ?? ''),
      value: typeof p.value === 'number' ? p.value : 0,
      color: p.color || (p as any).fill || '#888',
    }))
    .filter(e => e.value && Math.abs(e.value) > 0.01)
    .sort((a, b) => Math.abs(b.value) - Math.abs(a.value));

  if (!entries.length) return null;
  const total = entries.reduce((s, e) => s + e.value, 0);

  return (
    <div className="bg-[#0a0a0a] border border-white/15 rounded-lg p-2 text-xs shadow-xl min-w-[220px]">
      <div className="text-white/70 mb-1 font-medium">{label}</div>
      <div className="space-y-0.5">
        {entries.map(e => (
          <div key={e.name} className="flex justify-between gap-3 items-center">
            <span className="flex items-center gap-1.5 text-white/85 truncate">
              <span className="inline-block w-2 h-2 rounded-sm" style={{ backgroundColor: e.color }} />
              <span className="truncate">{e.name}</span>
            </span>
            <span className="tabular-nums text-white">
              {fmt(e.value)}
              {entries.length > 1 && (
                <span className="text-white/40 ml-1.5">
                  ({((e.value / total) * 100).toFixed(0)}%)
                </span>
              )}
            </span>
          </div>
        ))}
      </div>
      {entries.length > 1 && (
        <div className="flex justify-between mt-1.5 pt-1.5 border-t border-white/10">
          <span className="text-white/40">Total</span>
          <span className="tabular-nums text-white/90 font-medium">{fmt(total)}</span>
        </div>
      )}
    </div>
  );
}

export function TradingVolumeChart() {
  const [data, setData] = useState<VolumeData | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const [view, setView] = useState<View>('protocol');
  const [side, setSide] = useState<Side>('TOTAL');
  const [range, setRange] = useState<Range>('90d');
  const [unit, setUnit] = useState<Unit>('usd');
  // Number of markets to stack in topMarkets view — drops 'Others'.
  const [topN, setTopN] = useState<number>(15);

  useEffect(() => {
    fetch('/volume.json')
      .then(r => r.ok ? r.json() : Promise.reject(`HTTP ${r.status}`))
      .then((d: VolumeData) => setData(d))
      .catch(e => setErr(String(e)));
  }, []);

  const fmt = unit === 'usd' ? fmtUsd : fmtNum;
  const fieldKey = (s: 'pt' | 'yt' | 'total') =>
    unit === 'usd'
      ? (s === 'pt' ? 'ptUsd' : s === 'yt' ? 'ytUsd' : 'totalUsd')
      : (s === 'pt' ? 'ptUnderlying' : s === 'yt' ? 'ytUnderlying' : 'totalUnderlying');

  // Markets selected for the topMarkets view, ranked by USD total across the
  // entire history (most representative ordering). Memoized — only depends on
  // data + topN + side (which determines which series we rank by).
  const selectedMarkets = useMemo(() => {
    if (!data) return [] as string[];
    // Pre-sort by chosen side total over the *visible* range; that way "30d"
    // surfaces what's hot today, not historical heavyweights.
    const startIdx = Math.max(0, rangeStartIdx(data.dates, range));
    const seriesField = fieldKey(side === 'TOTAL' ? 'total' : side === 'PT' ? 'pt' : 'yt');
    const totals: { key: string; total: number }[] = Object.entries(data.byMarket).map(([mk, m]) => {
      const arr = (m as any)[seriesField] as number[];
      let sum = 0;
      for (let i = startIdx; i < arr.length; i++) sum += arr[i] || 0;
      return { key: mk, total: sum };
    });
    totals.sort((a, b) => b.total - a.total);
    return totals.slice(0, topN).filter(t => t.total > 0).map(t => t.key);
  }, [data, range, side, unit, topN]);

  const chartData = useMemo(() => {
    if (!data) return [];
    const startIdx = Math.max(0, rangeStartIdx(data.dates, range));
    const dates = data.dates.slice(startIdx);

    if (view === 'protocol') {
      const pt = (data.protocol as any)[fieldKey('pt')].slice(startIdx) as number[];
      const yt = (data.protocol as any)[fieldKey('yt')].slice(startIdx) as number[];
      let cum = 0;
      return dates.map((d, i) => {
        const v = side === 'PT' ? pt[i] : side === 'YT' ? yt[i] : pt[i] + yt[i];
        cum += v;
        return { date: d, PT: pt[i], YT: yt[i], Volume: v, Cumulative: cum };
      });
    }

    // topMarkets: stack each market as its own bar
    const seriesField = fieldKey(side === 'TOTAL' ? 'total' : side === 'PT' ? 'pt' : 'yt');
    return dates.map((d, i) => {
      const idx = startIdx + i;
      const row: Record<string, number | string> = { date: d };
      for (const mk of selectedMarkets) {
        const arr = (data.byMarket[mk] as any)[seriesField] as number[];
        row[mk] = (arr && arr[idx]) || 0;
      }
      return row;
    });
  }, [data, view, side, range, unit, selectedMarkets]);

  if (err) return <div className="text-red-400 text-sm p-4">Failed to load volume.json: {err}</div>;
  if (!data) return <div className="text-white/40 text-sm p-4">Loading trading volume…</div>;

  const meta = data.meta;
  const totals = unit === 'usd' ? meta.totalsUsd : meta.totalsUnderlying;

  return (
    <section className="rounded-2xl border border-white/10 bg-white/5 p-4 mb-6">
      <header className="flex items-baseline justify-between flex-wrap gap-2 mb-3">
        <div>
          <h2 className="text-sm uppercase tracking-wider text-white/60">Trading Volume</h2>
          <p className="text-xs text-white/40">
            {meta.dateRange[0]} → {meta.dateRange[1]}
            {' • '}
            cumulative {fmt(totals.total)} ({fmt(totals.pt)} PT + {fmt(totals.yt)} YT)
            <span className="text-white/30"> • prices: {meta.priceSources}</span>
          </p>
        </div>
        <div className="flex items-center gap-1 flex-wrap">
          {(['usd', 'underlying'] as Unit[]).map(u => (
            <button key={u} onClick={() => setUnit(u)}
              className={`text-xs px-3 py-1 rounded-lg border ${unit === u ? 'border-white/30 bg-white/10' : 'border-white/10 text-white/40'}`}>
              {u === 'usd' ? 'USD' : 'Underlying'}
            </button>
          ))}
          <span className="w-2" />
          {(['protocol', 'topMarkets'] as View[]).map(v => (
            <button key={v} onClick={() => setView(v)}
              className={`text-xs px-3 py-1 rounded-lg border ${view === v ? 'border-white/30 bg-white/10' : 'border-white/10 text-white/40'}`}>
              {v === 'protocol' ? 'Protocol' : 'Markets'}
            </button>
          ))}
          <span className="w-2" />
          {(['TOTAL', 'PT', 'YT'] as Side[]).map(s => (
            <button key={s} onClick={() => setSide(s)}
              className={`text-xs px-3 py-1 rounded-lg border ${side === s ? 'border-white/30 bg-white/10' : 'border-white/10 text-white/40'}`}>
              {s}
            </button>
          ))}
          <span className="w-2" />
          {(['30d', '90d', '1y', 'all'] as Range[]).map(r => (
            <button key={r} onClick={() => setRange(r)}
              className={`text-xs px-3 py-1 rounded-lg border ${range === r ? 'border-white/30 bg-white/10' : 'border-white/10 text-white/40'}`}>
              {r}
            </button>
          ))}
          {view === 'topMarkets' && (
            <>
              <span className="w-2" />
              <span className="text-[11px] text-white/40">Top</span>
              {[10, 15, 20].map(n => (
                <button key={n} onClick={() => setTopN(n)}
                  className={`text-xs px-2 py-1 rounded-lg border ${topN === n ? 'border-white/30 bg-white/10' : 'border-white/10 text-white/40'}`}>
                  {n}
                </button>
              ))}
            </>
          )}
        </div>
      </header>

      <ResponsiveContainer width="100%" height={360}>
        <ComposedChart data={chartData} margin={{ top: 10, right: 10, left: 0, bottom: 0 }}>
          <XAxis dataKey="date" tick={{ fill: '#888', fontSize: 11 }} />
          <YAxis yAxisId="left" tick={{ fill: '#888', fontSize: 11 }} tickFormatter={fmt} />
          {view === 'protocol' && (
            <YAxis yAxisId="right" orientation="right" tick={{ fill: '#888', fontSize: 11 }} tickFormatter={fmt} />
          )}
          <Tooltip content={<SortedTooltip fmt={fmt} />} />
          <Legend
            wrapperStyle={{ fontSize: 11, color: '#aaa' }}
            iconType="square"
            iconSize={8}
          />
          {view === 'protocol' ? (
            <>
              <Bar dataKey="Volume" fill="#38bdf8" fillOpacity={0.7} yAxisId="left" />
              <Line type="monotone" dataKey="Cumulative" stroke="#a78bfa" strokeWidth={2} dot={false} yAxisId="right" />
            </>
          ) : (
            // Stack one Bar per market, biggest at the bottom (last in render order)
            selectedMarkets.map((mk, i) => (
              <Bar
                key={mk}
                dataKey={mk}
                stackId="m"
                fill={COLORS[i % COLORS.length]}
                yAxisId="left"
              />
            ))
          )}
        </ComposedChart>
      </ResponsiveContainer>
    </section>
  );
}
