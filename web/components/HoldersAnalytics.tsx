'use client';
import { useEffect, useMemo, useState } from 'react';

type Row = {
  marketKey: string;
  ticker: string;
  leg: 'PT' | 'YT' | 'LP';
  nHolders: number;
  top1Pct: number;
  top5Pct: number;
  top10Pct: number;
  totalSupply: number;
  status: string | null;
  maturityDate: string | null;
};
type HoldersData = {
  meta: { generatedAt: string; snapshotDate: string; totalHolders: number; mintsCovered: number };
  rows: Row[];
};

type LegFilter = 'ALL' | 'PT' | 'YT' | 'LP';
type StatusFilter = 'all' | 'active';

function fmt(n: number) {
  if (Math.abs(n) >= 1e9) return `${(n / 1e9).toFixed(2)}B`;
  if (Math.abs(n) >= 1e6) return `${(n / 1e6).toFixed(2)}M`;
  if (Math.abs(n) >= 1e3) return `${(n / 1e3).toFixed(1)}K`;
  return n.toFixed(0);
}

function concentrationColor(pct: number): string {
  if (pct >= 80) return 'text-red-400';
  if (pct >= 60) return 'text-orange-400';
  if (pct >= 40) return 'text-yellow-400';
  return 'text-emerald-400';
}

export function HoldersAnalytics() {
  const [data, setData] = useState<HoldersData | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const [leg, setLeg] = useState<LegFilter>('ALL');
  const [status, setStatus] = useState<StatusFilter>('active');

  useEffect(() => {
    fetch('/holders.json')
      .then(r => (r.ok ? r.json() : Promise.reject(`HTTP ${r.status}`)))
      .then(setData)
      .catch(e => setErr(String(e)));
  }, []);

  const rows = useMemo(() => {
    if (!data) return [];
    return data.rows.filter(r =>
      (leg === 'ALL' || r.leg === leg) &&
      (status === 'all' || (status === 'active' && r.status === 'active'))
    );
  }, [data, leg, status]);

  if (err) return <div className="text-red-400 text-sm p-4">Failed to load holders.json: {err}</div>;
  if (!data) return <div className="text-white/40 text-sm p-4">Loading holders…</div>;

  const totalHolders = rows.reduce((s, r) => s + r.nHolders, 0);

  return (
    <section className="rounded-2xl border border-white/10 bg-white/5 p-4 mb-6">
      <header className="flex items-baseline justify-between flex-wrap gap-2 mb-3">
        <div>
          <h2 className="text-sm uppercase tracking-wider text-white/60">Holders</h2>
          <p className="text-xs text-white/40">
            Snapshot {data.meta.snapshotDate} • {rows.length} mints • {fmt(totalHolders)} holder rows
            <span className="text-white/30"> • via getProgramAccounts</span>
          </p>
        </div>
        <div className="flex items-center gap-1 flex-wrap">
          {(['ALL', 'PT', 'YT', 'LP'] as LegFilter[]).map(l => (
            <button key={l} onClick={() => setLeg(l)}
              className={`text-xs px-3 py-1 rounded-lg border ${leg === l ? 'border-white/30 bg-white/10' : 'border-white/10 text-white/40'}`}>
              {l}
            </button>
          ))}
          <span className="w-2" />
          {(['active', 'all'] as StatusFilter[]).map(s => (
            <button key={s} onClick={() => setStatus(s)}
              className={`text-xs px-3 py-1 rounded-lg border ${status === s ? 'border-white/30 bg-white/10' : 'border-white/10 text-white/40'}`}>
              {s === 'active' ? 'Active' : 'All'}
            </button>
          ))}
        </div>
      </header>

      <div className="overflow-x-auto">
        <table className="w-full text-xs">
          <thead className="text-white/40 border-b border-white/10">
            <tr>
              <th className="text-left py-2 font-normal">Market</th>
              <th className="text-left py-2 font-normal">Leg</th>
              <th className="text-right py-2 font-normal">Holders</th>
              <th className="text-right py-2 font-normal">Top 1</th>
              <th className="text-right py-2 font-normal">Top 5</th>
              <th className="text-right py-2 font-normal">Top 10</th>
              <th className="text-right py-2 font-normal">Supply</th>
            </tr>
          </thead>
          <tbody>
            {rows.map(r => (
              <tr key={`${r.marketKey}-${r.leg}`} className="border-b border-white/5">
                <td className="py-1.5 text-white/85">
                  <span className="truncate">{r.marketKey}</span>
                  {r.status !== 'active' && <span className="ml-1 text-white/30">·exp</span>}
                </td>
                <td className="py-1.5 text-white/60">{r.leg}</td>
                <td className="py-1.5 text-right tabular-nums text-white/85">{r.nHolders.toLocaleString()}</td>
                <td className={`py-1.5 text-right tabular-nums ${concentrationColor(r.top1Pct)}`}>{r.top1Pct.toFixed(1)}%</td>
                <td className={`py-1.5 text-right tabular-nums ${concentrationColor(r.top5Pct)}`}>{r.top5Pct.toFixed(1)}%</td>
                <td className={`py-1.5 text-right tabular-nums ${concentrationColor(r.top10Pct)}`}>{r.top10Pct.toFixed(1)}%</td>
                <td className="py-1.5 text-right tabular-nums text-white/60">{fmt(r.totalSupply)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </section>
  );
}
