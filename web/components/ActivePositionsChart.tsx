'use client';
import { useEffect, useMemo, useState } from 'react';
import {
  AreaChart, Area, XAxis, YAxis, Tooltip, ResponsiveContainer, Legend, TooltipProps,
} from 'recharts';

type Range = '30d' | '90d' | '1y' | 'all';
type Leg = 'PT' | 'YT' | 'LP' | 'SY';

type LegData = {
  byMarket: Record<string, number[]>;
  totals: number[];
};
type TickerData = {
  underlyingMint: string;
  latest: Partial<Record<Leg, number>>;
  legs: Partial<Record<Leg, LegData>>;
};
type ActivePositionsData = {
  meta: {
    dateRange: [string, string];
    tickers: { ticker: string; marketCount: number }[];
  };
  dates: string[];
  byTicker: Record<string, TickerData>;
};

const COLORS = [
  '#a78bfa', '#38bdf8', '#4ade80', '#f87171', '#fbbf24',
  '#fb923c', '#22d3ee', '#e879f9', '#a3e635', '#818cf8',
];

function fmtCount(n: number, ticker: string): string {
  const abs = Math.abs(n);
  if (abs >= 1e9) return `${(n / 1e9).toFixed(2)}B ${ticker}`;
  if (abs >= 1e6) return `${(n / 1e6).toFixed(2)}M ${ticker}`;
  if (abs >= 1e3) return `${(n / 1e3).toFixed(1)}K ${ticker}`;
  return `${n.toFixed(2)} ${ticker}`;
}

function rangeStartIdx(dates: string[], range: Range): number {
  if (range === 'all') return 0;
  const days = range === '30d' ? 30 : range === '90d' ? 90 : 365;
  const cutoff = new Date(new Date(dates[dates.length - 1]).getTime() - days * 86400_000).toISOString().slice(0, 10);
  return Math.max(0, dates.findIndex(d => d >= cutoff));
}

function SortedTooltip({ active, payload, label, ticker }: TooltipProps<number, string> & { ticker: string }) {
  if (!active || !payload?.length) return null;
  const entries = payload
    .map(p => ({ name: String(p.name ?? p.dataKey ?? ''), value: typeof p.value === 'number' ? p.value : 0, color: p.color || (p as any).fill || '#888' }))
    .filter(e => e.value > 0.001)
    .sort((a, b) => b.value - a.value);
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
              {fmtCount(e.value, ticker)}
              {entries.length > 1 && <span className="text-white/40 ml-1.5">({((e.value/total)*100).toFixed(0)}%)</span>}
            </span>
          </div>
        ))}
      </div>
      {entries.length > 1 && (
        <div className="flex justify-between mt-1.5 pt-1.5 border-t border-white/10">
          <span className="text-white/40">Total</span>
          <span className="tabular-nums text-white/90 font-medium">{fmtCount(total, ticker)}</span>
        </div>
      )}
    </div>
  );
}

export function ActivePositionsChart() {
  const [data, setData] = useState<ActivePositionsData | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const [ticker, setTicker] = useState<string>('USX');
  const [leg, setLeg] = useState<Leg>('PT');
  const [range, setRange] = useState<Range>('90d');

  useEffect(() => {
    fetch('/active_positions.json')
      .then(r => (r.ok ? r.json() : Promise.reject(`HTTP ${r.status}`)))
      .then(setData)
      .catch(e => setErr(String(e)));
  }, []);

  const tickers = data?.meta.tickers ?? [];
  const tickerData = data ? data.byTicker[ticker] : null;
  const legData = tickerData?.legs[leg];

  const chartData = useMemo(() => {
    if (!data || !tickerData || !legData) return [];
    const startIdx = rangeStartIdx(data.dates, range);
    const dates = data.dates.slice(startIdx);
    // For SY: single series (totals). For PT/YT/LP: stacked per market.
    if (leg === 'SY') {
      return dates.map((d, i) => ({ date: d, [ticker]: legData.totals[startIdx + i] || 0 }));
    }
    const markets = Object.keys(legData.byMarket);
    return dates.map((d, i) => {
      const row: Record<string, number | string> = { date: d };
      for (const mk of markets) {
        row[mk] = legData.byMarket[mk][startIdx + i] || 0;
      }
      return row;
    });
  }, [data, tickerData, legData, leg, ticker, range]);

  if (err) return <div className="text-red-400 text-sm p-4">Failed to load active_positions.json: {err}</div>;
  if (!data) return <div className="text-white/40 text-sm p-4">Loading active positions…</div>;
  if (!tickerData) return <div className="text-white/40 text-sm p-4">No data for {ticker}</div>;

  const latest = tickerData.latest;
  const legMarkets = leg === 'SY' ? [ticker] : Object.keys(legData?.byMarket ?? {});

  return (
    <section className="rounded-2xl border border-white/10 bg-white/5 p-4 mb-6">
      <header className="flex items-baseline justify-between flex-wrap gap-2 mb-3">
        <div>
          <h2 className="text-sm uppercase tracking-wider text-white/60">Active Positions</h2>
          <p className="text-xs text-white/40">
            {ticker} — current: PT {fmtCount(latest.PT ?? 0, ticker)} · YT {fmtCount(latest.YT ?? 0, ticker)} · LP {fmtCount(latest.LP ?? 0, ticker)} · SY {fmtCount(latest.SY ?? 0, ticker)}
            <span className="text-white/30"> • supplies in underlying units</span>
          </p>
        </div>
        <div className="flex items-center gap-1 flex-wrap">
          <select value={ticker} onChange={e => setTicker(e.target.value)}
            className="text-xs px-2 py-1 rounded-lg border border-white/10 bg-[#0a0a0a] text-white/80">
            {tickers.map(t => (
              <option key={t.ticker} value={t.ticker}>{t.ticker} ({t.marketCount}m)</option>
            ))}
          </select>
          <span className="w-2" />
          {(['PT', 'YT', 'LP', 'SY'] as Leg[]).map(l => (
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
        </div>
      </header>
      <ResponsiveContainer width="100%" height={320}>
        <AreaChart data={chartData} margin={{ top: 10, right: 10, left: 0, bottom: 0 }}>
          <XAxis dataKey="date" tick={{ fill: '#888', fontSize: 11 }} />
          <YAxis tick={{ fill: '#888', fontSize: 11 }} tickFormatter={n => fmtCount(n, '').trim()} />
          <Tooltip content={<SortedTooltip ticker={ticker} />} />
          <Legend wrapperStyle={{ fontSize: 11, color: '#aaa' }} iconType="square" iconSize={8} />
          {legMarkets.map((key, i) => (
            <Area
              key={key} type="monotone" dataKey={key}
              stackId={leg === 'SY' ? undefined : 'm'}
              stroke={COLORS[i % COLORS.length]}
              fill={COLORS[i % COLORS.length] + '66'}
            />
          ))}
        </AreaChart>
      </ResponsiveContainer>
    </section>
  );
}
