'use client';
import { useEffect, useMemo, useState } from 'react';
import {
  ComposedChart, Bar, Line, XAxis, YAxis, Tooltip, ResponsiveContainer, Legend,
} from 'recharts';

type Side = 'PT' | 'YT' | 'TOTAL';
type Range = '30d' | '90d' | '1y' | 'all';
type View = 'protocol' | 'topMarkets';

type VolumeData = {
  meta: {
    generatedAt: string;
    dateRange: [string, string];
    totals: { pt: number; yt: number; total: number };
    source: string;
  };
  dates: string[];
  protocol: { pt: number[]; yt: number[]; total: number[] };
  byMarket: Record<string, { ticker: string; pt: number[]; yt: number[]; total: number[] }>;
  topMarkets: { marketKey: string; ticker: string; total: number }[];
};

const COLORS = [
  '#38bdf8', '#a78bfa', '#4ade80', '#f87171', '#fbbf24',
  '#fb923c', '#22d3ee', '#e879f9', '#a3e635', '#818cf8',
];

function fmtNum(n: number) {
  if (n >= 1e9) return `${(n / 1e9).toFixed(2)}B`;
  if (n >= 1e6) return `${(n / 1e6).toFixed(2)}M`;
  if (n >= 1e3) return `${(n / 1e3).toFixed(1)}K`;
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

export function TradingVolumeChart() {
  const [data, setData] = useState<VolumeData | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const [view, setView] = useState<View>('protocol');
  const [side, setSide] = useState<Side>('TOTAL');
  const [range, setRange] = useState<Range>('90d');

  useEffect(() => {
    fetch('/volume.json')
      .then(r => r.ok ? r.json() : Promise.reject(`HTTP ${r.status}`))
      .then((d: VolumeData) => setData(d))
      .catch(e => setErr(String(e)));
  }, []);

  const chartData = useMemo(() => {
    if (!data) return [];
    const startIdx = Math.max(0, rangeStartIdx(data.dates, range));
    const dates = data.dates.slice(startIdx);

    if (view === 'protocol') {
      const ptSlice = data.protocol.pt.slice(startIdx);
      const ytSlice = data.protocol.yt.slice(startIdx);
      let cum = 0;
      return dates.map((d, i) => {
        const v = side === 'PT' ? ptSlice[i] : side === 'YT' ? ytSlice[i] : ptSlice[i] + ytSlice[i];
        cum += v;
        return { date: d, PT: ptSlice[i], YT: ytSlice[i], Volume: v, Cumulative: cum };
      });
    }

    // topMarkets: stack top 5 + Others
    const top = data.topMarkets.slice(0, 5);
    const topKeys = new Set(top.map(m => m.marketKey));
    return dates.map((d, i) => {
      const idx = startIdx + i;
      const row: Record<string, number | string> = { date: d };
      let others = 0;
      for (const [mk, series] of Object.entries(data.byMarket)) {
        const arr = side === 'PT' ? series.pt : side === 'YT' ? series.yt : series.total;
        const val = arr[idx] || 0;
        if (topKeys.has(mk)) {
          row[mk] = val;
        } else {
          others += val;
        }
      }
      row['Others'] = others;
      return row;
    });
  }, [data, view, side, range]);

  if (err) return <div className="text-red-400 text-sm p-4">Failed to load volume.json: {err}</div>;
  if (!data) return <div className="text-white/40 text-sm p-4">Loading trading volume…</div>;

  const meta = data.meta;
  const topKeys = data.topMarkets.slice(0, 5).map(m => m.marketKey);

  return (
    <section className="rounded-2xl border border-white/10 bg-white/5 p-4 mb-6">
      <header className="flex items-baseline justify-between flex-wrap gap-2 mb-3">
        <div>
          <h2 className="text-sm uppercase tracking-wider text-white/60">Trading Volume</h2>
          <p className="text-xs text-white/40">
            {meta.dateRange[0]} → {meta.dateRange[1]}
            {' • '}
            cumulative {fmtNum(meta.totals.total)} ({fmtNum(meta.totals.pt)} PT + {fmtNum(meta.totals.yt)} YT)
          </p>
        </div>
        <div className="flex items-center gap-1 flex-wrap">
          {(['protocol', 'topMarkets'] as View[]).map(v => (
            <button key={v} onClick={() => setView(v)}
              className={`text-xs px-3 py-1 rounded-lg border ${view === v ? 'border-white/30 bg-white/10' : 'border-white/10 text-white/40'}`}>
              {v === 'protocol' ? 'Protocol' : 'Top markets'}
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
        </div>
      </header>

      <ResponsiveContainer width="100%" height={320}>
        <ComposedChart data={chartData} margin={{ top: 10, right: 10, left: 0, bottom: 0 }}>
          <XAxis dataKey="date" tick={{ fill: '#888', fontSize: 11 }} />
          <YAxis yAxisId="left" tick={{ fill: '#888', fontSize: 11 }} tickFormatter={fmtNum} />
          {view === 'protocol' && (
            <YAxis yAxisId="right" orientation="right" tick={{ fill: '#888', fontSize: 11 }} tickFormatter={fmtNum} />
          )}
          <Tooltip
            contentStyle={{ backgroundColor: '#0a0a0a', border: '1px solid #333', fontSize: 12 }}
            formatter={(val: number) => fmtNum(val)}
          />
          <Legend wrapperStyle={{ fontSize: 11, color: '#aaa' }} />
          {view === 'protocol' ? (
            <>
              <Bar dataKey="Volume" fill="#38bdf8" fillOpacity={0.7} yAxisId="left" />
              <Line type="monotone" dataKey="Cumulative" stroke="#a78bfa" strokeWidth={2} dot={false} yAxisId="right" />
            </>
          ) : (
            <>
              {topKeys.map((mk, i) => (
                <Bar key={mk} dataKey={mk} stackId="m" fill={COLORS[i % COLORS.length]} yAxisId="left" />
              ))}
              <Bar dataKey="Others" stackId="m" fill="#444" yAxisId="left" />
            </>
          )}
        </ComposedChart>
      </ResponsiveContainer>
    </section>
  );
}
