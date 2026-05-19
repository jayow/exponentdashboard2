'use client';
import { useEffect, useMemo, useState } from 'react';
import {
  AreaChart, Area, XAxis, YAxis, Tooltip, ResponsiveContainer, Legend, TooltipProps,
} from 'recharts';

type Range = '30d' | '90d' | '1y' | 'all';
type View = 'protocol' | 'markets';
type Leg = 'TOTAL' | 'PT' | 'YT' | 'LP';

type OiData = {
  meta: {
    dateRange: [string, string];
    currentPtUsd: number;
    currentYtUsd: number;
    currentLpUsd?: number;
    source: string;
  };
  dates: string[];
  protocol: { pt: number[]; yt: number[]; lp?: number[]; total: number[] };
  byMarket: Record<string, { ticker: string; pt: number[]; yt: number[]; lp?: number[]; total: number[] }>;
  topMarkets: { marketKey: string; ticker: string; oiUsdNow: number }[];
};

const COLORS = [
  '#a78bfa', '#38bdf8', '#4ade80', '#f87171', '#fbbf24',
  '#fb923c', '#22d3ee', '#e879f9', '#a3e635', '#818cf8',
  '#facc15', '#34d399', '#f472b6', '#60a5fa', '#fcd34d',
];

function fmtUsd(n: number) {
  if (Math.abs(n) >= 1e9) return `$${(n / 1e9).toFixed(2)}B`;
  if (Math.abs(n) >= 1e6) return `$${(n / 1e6).toFixed(2)}M`;
  if (Math.abs(n) >= 1e3) return `$${(n / 1e3).toFixed(1)}K`;
  return `$${n.toFixed(2)}`;
}

function rangeStartIdx(dates: string[], range: Range) {
  if (range === 'all') return 0;
  const days = range === '30d' ? 30 : range === '90d' ? 90 : 365;
  const cutoff = new Date(new Date(dates[dates.length - 1]).getTime() - days * 86400_000).toISOString().slice(0, 10);
  return Math.max(0, dates.findIndex(d => d >= cutoff));
}

function SortedTooltip({ active, payload, label }: TooltipProps<number, string>) {
  if (!active || !payload?.length) return null;
  const entries = payload
    .map(p => ({ name: String(p.name ?? p.dataKey ?? ''), value: typeof p.value === 'number' ? p.value : 0, color: p.color || (p as any).fill || '#888' }))
    .filter(e => e.value > 0.01).sort((a, b) => b.value - a.value);
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
              {fmtUsd(e.value)}
              {entries.length > 1 && <span className="text-white/40 ml-1.5">({((e.value/total)*100).toFixed(0)}%)</span>}
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

export function OpenInterestChart() {
  const [data, setData] = useState<OiData | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const [view, setView] = useState<View>('protocol');
  const [leg, setLeg] = useState<Leg>('TOTAL');
  const [range, setRange] = useState<Range>('90d');
  const [topN, setTopN] = useState<number>(15);

  useEffect(() => {
    fetch('/open_interest.json')
      .then(r => r.ok ? r.json() : Promise.reject(`HTTP ${r.status}`))
      .then(setData).catch(e => setErr(String(e)));
  }, []);

  const seriesKey: 'pt' | 'yt' | 'lp' | 'total' =
    leg === 'PT' ? 'pt' : leg === 'YT' ? 'yt' : leg === 'LP' ? 'lp' : 'total';

  const selectedMarkets = useMemo(() => {
    if (!data) return [] as string[];
    const totals = Object.entries(data.byMarket).map(([mk, m]) => {
      const arr = (m as any)[seriesKey] as number[] | undefined;
      const v = arr?.[arr.length - 1] || 0;
      return { key: mk, total: v };
    });
    totals.sort((a, b) => b.total - a.total);
    return totals.slice(0, topN).filter(t => t.total > 0).map(t => t.key);
  }, [data, topN, leg]);

  const chartData = useMemo(() => {
    if (!data) return [];
    const startIdx = rangeStartIdx(data.dates, range);
    const dates = data.dates.slice(startIdx);
    if (view === 'protocol') {
      const pt = data.protocol.pt.slice(startIdx);
      const yt = data.protocol.yt.slice(startIdx);
      const lp = (data.protocol.lp ?? Array(data.dates.length).fill(0)).slice(startIdx);
      return dates.map((d, i) => ({ date: d, PT: pt[i] || 0, YT: yt[i] || 0, LP: lp[i] || 0, Total: (pt[i] || 0) + (yt[i] || 0) + (lp[i] || 0) }));
    }
    return dates.map((d, i) => {
      const idx = startIdx + i;
      const row: Record<string, number | string> = { date: d };
      for (const mk of selectedMarkets) {
        const arr = (data.byMarket[mk] as any)[seriesKey] as number[] | undefined;
        row[mk] = arr?.[idx] || 0;
      }
      return row;
    });
  }, [data, view, leg, range, selectedMarkets]);

  if (err) return <div className="text-red-400 text-sm p-4">Failed to load open_interest.json: {err}</div>;
  if (!data) return <div className="text-white/40 text-sm p-4">Loading open interest…</div>;

  return (
    <section className="rounded-2xl border border-white/10 bg-white/5 p-4 mb-6">
      <header className="flex items-baseline justify-between flex-wrap gap-2 mb-3">
        <div>
          <h2 className="text-sm uppercase tracking-wider text-white/60">Open Interest</h2>
          <p className="text-xs text-white/40">
            current: PT {fmtUsd(data.meta.currentPtUsd)} + YT {fmtUsd(data.meta.currentYtUsd)}
            {(data.meta.currentLpUsd ?? 0) > 0 ? <> + LP {fmtUsd(data.meta.currentLpUsd!)}</> : null}
            {' '}= {fmtUsd(data.meta.currentPtUsd + data.meta.currentYtUsd + (data.meta.currentLpUsd ?? 0))}
            <span className="text-white/30"> • {data.meta.source}</span>
          </p>
        </div>
        <div className="flex items-center gap-1 flex-wrap">
          {(['protocol', 'markets'] as View[]).map(v => (
            <button key={v} onClick={() => setView(v)}
              className={`text-xs px-3 py-1 rounded-lg border ${view === v ? 'border-white/30 bg-white/10' : 'border-white/10 text-white/40'}`}>
              {v === 'protocol' ? 'Protocol' : 'Markets'}
            </button>
          ))}
          <span className="w-2" />
          {((['TOTAL', 'PT', 'YT', 'LP'] as Leg[]).filter(
            l => l !== 'LP' || (data.meta.currentLpUsd ?? 0) > 0
          )).map(l => (
            <button key={l} onClick={() => setLeg(l)}
              className={`text-xs px-3 py-1 rounded-lg border ${leg === l ? 'border-white/30 bg-white/10' : 'border-white/10 text-white/40'}`}>
              {l}
            </button>
          ))}
          <span className="w-2" />
          {(['30d', '90d', '1y', 'all'] as Range[]).map(r => (
            <button key={r} onClick={() => setRange(r)}
              className={`text-xs px-3 py-1 rounded-lg border ${range === r ? 'border-white/30 bg-white/10' : 'border-white/10 text-white/40'}`}>
              {r}
            </button>
          ))}
          {view === 'markets' && (
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
      <ResponsiveContainer width="100%" height={320}>
        <AreaChart data={chartData} margin={{ top: 10, right: 10, left: 0, bottom: 0 }}>
          <XAxis dataKey="date" tick={{ fill: '#888', fontSize: 11 }} />
          <YAxis tick={{ fill: '#888', fontSize: 11 }} tickFormatter={fmtUsd} />
          <Tooltip content={<SortedTooltip />} />
          <Legend wrapperStyle={{ fontSize: 11, color: '#aaa' }} iconType="square" iconSize={8} />
          {view === 'protocol' ? (
            leg === 'TOTAL' ? (
              <>
                <Area type="monotone" dataKey="PT" stackId="1" stroke="#a78bfa" fill="#a78bfa66" />
                <Area type="monotone" dataKey="YT" stackId="1" stroke="#38bdf8" fill="#38bdf866" />
                <Area type="monotone" dataKey="LP" stackId="1" stroke="#4ade80" fill="#4ade8066" />
              </>
            ) : leg === 'PT' ? (
              <Area type="monotone" dataKey="PT" stroke="#a78bfa" fill="#a78bfa66" />
            ) : leg === 'YT' ? (
              <Area type="monotone" dataKey="YT" stroke="#38bdf8" fill="#38bdf866" />
            ) : (
              <Area type="monotone" dataKey="LP" stroke="#4ade80" fill="#4ade8066" />
            )
          ) : (
            selectedMarkets.map((mk, i) => (
              <Area key={mk} type="monotone" dataKey={mk} stackId="m" stroke={COLORS[i % COLORS.length]} fill={COLORS[i % COLORS.length] + '66'} />
            ))
          )}
        </AreaChart>
      </ResponsiveContainer>
    </section>
  );
}
