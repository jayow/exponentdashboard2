'use client';
import { useEffect, useMemo, useState } from 'react';

type Mode = 'snapshot' | 'rolling30d';
type Metric = 'volume' | 'tvl';

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
type ShareData = {
  meta: { generatedAt: string };
  snapshot: SnapshotRow[];
  rolling30d: RollingRow[];
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

export function MarketShare() {
  const [data, setData] = useState<ShareData | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const [mode, setMode] = useState<Mode>('rolling30d');
  const [metric, setMetric] = useState<Metric>('volume');

  useEffect(() => {
    fetch('/market_share.json')
      .then(r => (r.ok ? r.json() : Promise.reject(`HTTP ${r.status}`)))
      .then(setData)
      .catch(e => setErr(String(e)));
  }, []);

  const rows = useMemo(() => {
    if (!data) return [] as { ticker: string; value: number; pct: number }[];
    if (mode === 'snapshot') {
      return data.snapshot.map(r => ({
        ticker: r.ticker,
        value: metric === 'volume' ? r.volumeUsd : r.tvlUsd,
        pct: metric === 'volume' ? r.volumeSharePct : r.tvlSharePct,
      })).filter(r => r.value > 0).sort((a, b) => b.value - a.value);
    }
    return data.rolling30d.map(r => ({
      ticker: r.ticker,
      value: metric === 'volume' ? r.volumeUsd30d : r.tvlUsdAvg30d,
      pct: metric === 'volume' ? r.volumeShare30dPct : r.tvlShare30dPct,
    })).filter(r => r.value > 0).sort((a, b) => b.value - a.value);
  }, [data, mode, metric]);

  const total = useMemo(() => rows.reduce((s, r) => s + r.value, 0), [rows]);

  if (err) return <div className="text-red-400 text-sm p-4">Failed to load market_share.json: {err}</div>;
  if (!data) return <div className="text-white/40 text-sm p-4">Loading market share…</div>;

  const headerLabel = metric === 'volume'
    ? mode === 'snapshot' ? 'Volume (today)' : 'Volume (30d)'
    : mode === 'snapshot' ? 'TVL (today)' : 'TVL (30d avg)';

  return (
    <section className="rounded-2xl border border-white/10 bg-white/5 p-4 mb-6">
      <header className="flex items-baseline justify-between flex-wrap gap-2 mb-3">
        <div>
          <h2 className="text-sm uppercase tracking-wider text-white/60">Market Share</h2>
          <p className="text-xs text-white/40">
            {headerLabel} • {rows.length} markets • total {fmtUsd(total)}
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
          {(['snapshot', 'rolling30d'] as Mode[]).map(m => (
            <button key={m} onClick={() => setMode(m)}
              className={`text-xs px-3 py-1 rounded-lg border ${mode === m ? 'border-white/30 bg-white/10' : 'border-white/10 text-white/40'}`}>
              {m === 'snapshot' ? 'Today' : '30d'}
            </button>
          ))}
        </div>
      </header>

      <div className="flex h-3 w-full rounded overflow-hidden border border-white/10 mb-4">
        {rows.slice(0, 15).map((r, i) => (
          <div key={r.ticker}
            title={`${r.ticker} • ${fmtUsd(r.value)} (${r.pct.toFixed(1)}%)`}
            style={{ width: `${r.pct}%`, backgroundColor: COLORS[i % COLORS.length] }} />
        ))}
      </div>

      <div className="grid grid-cols-1 sm:grid-cols-2 gap-x-6 gap-y-1 text-xs">
        {rows.map((r, i) => (
          <div key={r.ticker} className="flex items-center justify-between border-b border-white/5 py-1">
            <span className="flex items-center gap-2 truncate">
              <span className="inline-block w-2 h-2 rounded-sm shrink-0"
                style={{ backgroundColor: i < 15 ? COLORS[i % COLORS.length] : '#444' }} />
              <span className="text-white/80 truncate">{r.ticker}</span>
            </span>
            <span className="flex items-center gap-3 tabular-nums shrink-0">
              <span className="text-white/60">{fmtUsd(r.value)}</span>
              <span className="text-white/40 w-12 text-right">{r.pct.toFixed(2)}%</span>
            </span>
          </div>
        ))}
      </div>
    </section>
  );
}
