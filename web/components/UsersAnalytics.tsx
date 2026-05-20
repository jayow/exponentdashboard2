'use client';
import { useEffect, useMemo, useState } from 'react';
import {
  AreaChart, Area, BarChart, Bar, XAxis, YAxis, Tooltip, ResponsiveContainer, ComposedChart, Line,
} from 'recharts';

type TopWallet = {
  signer: string; nSwaps: number; totalVolumeUsd: number;
  nMarkets: number; nTickers: number;
  firstSeen: string; lastSeen: string; activeSpanDays: number;
  actions: { buyYt: number; sellYt: number; buyPt: number; sellPt: number };
};
type UsersData = {
  meta: { generatedAt: string };
  headline: {
    totalWallets: number; oneSwap: number; casualWallets: number;
    activeWallets: number; powerWallets: number;
    lifetimeVolumeUsd: number; avgActiveSpanDays: number; maxSwapsByOneWallet: number;
  };
  concentration30d: {
    recentWallets: number; totalVolumeUsd: number;
    top10SharePct: number; top100SharePct: number;
  };
  growth: {
    dates: string[];
    activeWallets: number[]; newWallets: number[]; cumulativeWallets: number[];
    swaps: number[]; volumeUsd: number[];
  };
  holdersGrowth?: {
    dates: string[];
    PT: number[]; YT: number[]; LP: number[];
    sources: { PT: string[]; YT: string[]; LP: string[] };
  };
  topWallets: TopWallet[];
};
type Range = '30d' | '90d' | '1y' | 'all';
type Metric = 'cumulative' | 'newDaily' | 'active' | 'holders';

function fmt(n: number) {
  if (Math.abs(n) >= 1e9) return `${(n / 1e9).toFixed(2)}B`;
  if (Math.abs(n) >= 1e6) return `${(n / 1e6).toFixed(2)}M`;
  if (Math.abs(n) >= 1e3) return `${(n / 1e3).toFixed(1)}K`;
  return n.toFixed(0);
}
function fmtUsd(n: number) { return `$${fmt(n)}`; }
function rangeStartIdx(dates: string[], range: Range) {
  if (range === 'all') return 0;
  const days = range === '30d' ? 30 : range === '90d' ? 90 : 365;
  const cutoff = new Date(new Date(dates[dates.length - 1]).getTime() - days * 86400_000).toISOString().slice(0, 10);
  return Math.max(0, dates.findIndex(d => d >= cutoff));
}

export function UsersAnalytics() {
  const [data, setData] = useState<UsersData | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const [metric, setMetric] = useState<Metric>('cumulative');
  const [range, setRange] = useState<Range>('all');

  useEffect(() => {
    fetch('/users.json')
      .then(r => (r.ok ? r.json() : Promise.reject(`HTTP ${r.status}`)))
      .then(setData)
      .catch(e => setErr(String(e)));
  }, []);

  const chartData = useMemo(() => {
    if (!data) return [];
    if (metric === 'holders' && data.holdersGrowth) {
      const hg = data.holdersGrowth;
      const start = rangeStartIdx(hg.dates, range);
      return hg.dates.slice(start).map((d, i) => ({
        date: d,
        PT: hg.PT[start + i] || 0,
        YT: hg.YT[start + i] || 0,
        LP: hg.LP[start + i] || 0,
      }));
    }
    const start = rangeStartIdx(data.growth.dates, range);
    return data.growth.dates.slice(start).map((d, i) => ({
      date: d,
      Cumulative: data.growth.cumulativeWallets[start + i],
      NewWallets: data.growth.newWallets[start + i],
      ActiveWallets: data.growth.activeWallets[start + i],
    }));
  }, [data, range, metric]);

  if (err) return <div className="text-red-400 text-sm p-4">Failed to load users.json: {err}</div>;
  if (!data) return <div className="text-white/40 text-sm p-4">Loading users…</div>;

  const h = data.headline;
  const c = data.concentration30d;

  return (
    <section className="rounded-2xl border border-white/10 bg-white/5 p-4 mb-6">
      <header className="flex items-baseline justify-between flex-wrap gap-2 mb-3">
        <div>
          <h2 className="text-sm uppercase tracking-wider text-white/60">Users</h2>
          <p className="text-xs text-white/40">
            {fmt(h.totalWallets)} unique wallets • 30d top-10 = {c.top10SharePct.toFixed(0)}% of volume • top-100 = {c.top100SharePct.toFixed(0)}%
          </p>
        </div>
        <div className="flex items-center gap-1 flex-wrap">
          {(['cumulative', 'newDaily', 'active', 'holders'] as Metric[]).map(m => (
            <button key={m} onClick={() => setMetric(m)}
              className={`text-xs px-3 py-1 rounded-lg border ${metric === m ? 'border-white/30 bg-white/10' : 'border-white/10 text-white/40'}`}>
              {m === 'cumulative' ? 'Cumulative' : m === 'newDaily' ? 'New daily' : m === 'active' ? 'Daily active' : 'PT/YT/LP holders'}
            </button>
          ))}
          <span className="w-2" />
          {(['30d', '90d', '1y', 'all'] as Range[]).map(r => (
            <button key={r} onClick={() => setRange(r)}
              className={`text-xs px-3 py-1 rounded-lg border ${range === r ? 'border-white/30 bg-white/10' : 'border-white/10 text-white/40'}`}>
              {r === 'all' ? 'All' : r.toUpperCase()}
            </button>
          ))}
        </div>
      </header>

      {/* Cohort breakdown cards */}
      <div className="grid grid-cols-2 sm:grid-cols-5 gap-2 mb-4 text-xs">
        {[
          { label: '1 swap', value: h.oneSwap, sub: 'one-and-done' },
          { label: '2-9 swaps', value: h.casualWallets, sub: 'casual' },
          { label: '10-99 swaps', value: h.activeWallets, sub: 'active' },
          { label: '100+ swaps', value: h.powerWallets, sub: 'power users' },
          { label: 'max by 1 wallet', value: h.maxSwapsByOneWallet, sub: 'top trader' },
        ].map(s => (
          <div key={s.label} className="rounded-lg border border-white/10 p-2">
            <div className="text-[10px] uppercase text-white/40">{s.label}</div>
            <div className="text-base font-semibold tabular-nums text-white mt-0.5">{s.value.toLocaleString()}</div>
            <div className="text-[10px] text-white/30">{s.sub}</div>
          </div>
        ))}
      </div>

      {/* Growth chart */}
      <ResponsiveContainer width="100%" height={240}>
        {metric === 'cumulative' ? (
          <AreaChart data={chartData as any} margin={{ top: 10, right: 10, left: 0, bottom: 0 }}>
            <XAxis dataKey="date" tick={{ fill: '#888', fontSize: 11 }} />
            <YAxis tick={{ fill: '#888', fontSize: 11 }} tickFormatter={n => fmt(n)} />
            <Tooltip contentStyle={{ background: '#0a0a0a', border: '1px solid rgba(255,255,255,0.15)', fontSize: 11 }} formatter={(v: any) => fmt(Number(v))} />
            <Area type="monotone" dataKey="Cumulative" stroke="#a78bfa" fill="#a78bfa66" />
          </AreaChart>
        ) : metric === 'holders' ? (
          <ComposedChart data={chartData as any} margin={{ top: 10, right: 10, left: 0, bottom: 0 }}>
            <XAxis dataKey="date" tick={{ fill: '#888', fontSize: 11 }} />
            <YAxis tick={{ fill: '#888', fontSize: 11 }} tickFormatter={n => fmt(n)} />
            <Tooltip contentStyle={{ background: '#0a0a0a', border: '1px solid rgba(255,255,255,0.15)', fontSize: 11 }} formatter={(v: any) => fmt(Number(v))} />
            <Line type="monotone" dataKey="PT" stroke="#a78bfa" strokeWidth={1.5} dot={false} isAnimationActive={false} />
            <Line type="monotone" dataKey="YT" stroke="#38bdf8" strokeWidth={1.5} dot={false} isAnimationActive={false} />
            <Line type="monotone" dataKey="LP" stroke="#4ade80" strokeWidth={1.5} dot={false} isAnimationActive={false} />
          </ComposedChart>
        ) : (
          <BarChart data={chartData as any} margin={{ top: 10, right: 10, left: 0, bottom: 0 }}>
            <XAxis dataKey="date" tick={{ fill: '#888', fontSize: 11 }} />
            <YAxis tick={{ fill: '#888', fontSize: 11 }} tickFormatter={n => fmt(n)} />
            <Tooltip contentStyle={{ background: '#0a0a0a', border: '1px solid rgba(255,255,255,0.15)', fontSize: 11 }} formatter={(v: any) => fmt(Number(v))} />
            <Bar dataKey={metric === 'newDaily' ? 'NewWallets' : 'ActiveWallets'} fill="#38bdf8" />
          </BarChart>
        )}
      </ResponsiveContainer>
      {metric === 'holders' && (
        <div className="text-[10px] text-white/30 mt-1">
          PT reconstructed from SPL transfers (may overcount due to uncrawled wallet→wallet transfers); YT/LP reconstructed from Anchor instruction logs. Today's value anchored to on-chain snapshot.
        </div>
      )}

      {/* Top wallets leaderboard */}
      <div className="mt-4">
        <div className="text-xs text-white/40 mb-2">Top 20 wallets by volume</div>
        <div className="overflow-x-auto">
          <table className="w-full text-xs">
            <thead className="text-white/40 border-b border-white/10">
              <tr>
                <th className="text-left py-1 font-normal">#</th>
                <th className="text-left py-1 font-normal">Wallet</th>
                <th className="text-right py-1 font-normal">Volume</th>
                <th className="text-right py-1 font-normal">Swaps</th>
                <th className="text-right py-1 font-normal">Markets</th>
                <th className="text-right py-1 font-normal">Span</th>
                <th className="text-left py-1 font-normal pl-3">Mix</th>
              </tr>
            </thead>
            <tbody>
              {data.topWallets.slice(0, 20).map((w, i) => {
                const totalActions = w.actions.buyYt + w.actions.sellYt + w.actions.buyPt + w.actions.sellPt;
                const ytPct = totalActions ? Math.round(100 * (w.actions.buyYt + w.actions.sellYt) / totalActions) : 0;
                return (
                  <tr key={w.signer} className="border-b border-white/5">
                    <td className="py-1 text-white/40">{i + 1}</td>
                    <td className="py-1 font-mono text-white/80">
                      <a href={`/wallet/?addr=${w.signer}`} className="hover:text-white">
                        {w.signer.slice(0, 4)}…{w.signer.slice(-4)}
                      </a>
                      <a href={`https://solscan.io/account/${w.signer}`} target="_blank" rel="noopener noreferrer" className="ml-1 text-white/20 hover:text-white/60 text-[10px]">↗</a>
                    </td>
                    <td className="py-1 text-right tabular-nums text-white/85">{fmtUsd(w.totalVolumeUsd)}</td>
                    <td className="py-1 text-right tabular-nums text-white/60">{w.nSwaps.toLocaleString()}</td>
                    <td className="py-1 text-right tabular-nums text-white/60">{w.nMarkets}</td>
                    <td className="py-1 text-right tabular-nums text-white/60">{w.activeSpanDays}d</td>
                    <td className="py-1 pl-3 text-white/50">
                      <span className="text-sky-400">{100 - ytPct}% PT</span>
                      {' · '}
                      <span className="text-violet-400">{ytPct}% YT</span>
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      </div>
    </section>
  );
}
