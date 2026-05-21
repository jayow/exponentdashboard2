'use client';
import { useEffect, useMemo, useState } from 'react';
import {
  AreaChart, Area, BarChart, Bar, XAxis, YAxis, Tooltip, ResponsiveContainer, ComposedChart, Line,
} from 'recharts';
import { platformOfTicker } from '@/lib/colors';

type Wallet = {
  wallet: string;
  holdingUsd: number; ptUsd: number; ytUsd: number; lpUsd: number;
  activeMarkets: number;
  unclaimedUsd: number; nClaims: number;
  nSwaps: number; lifetimeVolumeUsd: number; lifetimeMarkets: number;
  actions: { buyYt: number; sellYt: number; buyPt: number; sellPt: number };
  firstSeen: string | null; lastSeen: string | null;
  activeSpanDays: number;
  isPool?: boolean;
};
type Cohort = {
  cohort: string; count: number; totalHoldingsUsd: number; trades: number;
  avgSwaps: number; avgMarkets: number; active30d: number;
};
type WhaleEvent = {
  signature: string; date: string; signer: string;
  market: string; ticker: string; platform: string;
  action: string; direction: string; usd: number;
};
type UsersData = {
  meta: { generatedAt: string };
  headline: {
    totalWallets: number; oneSwap: number; casualWallets: number;
    activeWallets: number; powerWallets: number;
    lifetimeVolumeUsd: number; avgActiveSpanDays: number; maxSwapsByOneWallet: number;
  };
  concentration30d: { recentWallets: number; totalVolumeUsd: number; top10SharePct: number; top100SharePct: number };
  growth: {
    dates: string[];
    activeWallets: number[]; newWallets: number[]; cumulativeWallets: number[];
    swaps: number[]; volumeUsd: number[];
  };
  holdersGrowth?: { dates: string[]; PT: number[]; YT: number[]; LP: number[] };
  wallets: Wallet[];
  cohorts: Cohort[];
  retention: { weeks: string[]; new: number[]; returning: number[]; active: number[] };
  whaleEvents: WhaleEvent[];
};

type HolderRow = {
  marketKey: string; ticker: string; leg: 'PT' | 'YT' | 'LP';
  nHolders: number; top1Pct: number; top5Pct: number; top10Pct: number;
  totalSupply: number; status: string | null; maturityDate: string | null;
  isTest?: boolean;
};
type HoldersData = { rows: HolderRow[] };

type View = 'leaderboard' | 'cohorts' | 'swaps' | 'growth' | 'retention' | 'whales' | 'concentration';
type Range = '30d' | '90d' | '1y' | 'all';
type SortKey = 'holdingUsd' | 'unclaimedUsd' | 'nSwaps' | 'lifetimeMarkets' | 'firstSeen' | 'lastSeen';
type LegFilter = 'ALL' | 'PT' | 'YT' | 'LP';
type StatusFilter = 'all' | 'active';

function fmt(n: number) {
  if (Math.abs(n) >= 1e9) return `${(n / 1e9).toFixed(2)}B`;
  if (Math.abs(n) >= 1e6) return `${(n / 1e6).toFixed(2)}M`;
  if (Math.abs(n) >= 1e3) return `${(n / 1e3).toFixed(1)}K`;
  return n.toFixed(0);
}
function fmtUsd(n: number) { return n ? `$${fmt(n)}` : '–'; }
function rangeStartIdx(dates: string[], range: Range) {
  if (range === 'all' || !dates.length) return 0;
  const days = range === '30d' ? 30 : range === '90d' ? 90 : 365;
  const cutoff = new Date(new Date(dates[dates.length - 1]).getTime() - days * 86400_000).toISOString().slice(0, 10);
  return Math.max(0, dates.findIndex(d => d >= cutoff));
}
function concentrationColor(pct: number): string {
  if (pct >= 80) return 'text-red-400';
  if (pct >= 60) return 'text-orange-400';
  if (pct >= 40) return 'text-yellow-400';
  return 'text-emerald-400';
}
function daysSinceUtc(iso: string | null): number | null {
  if (!iso) return null;
  return Math.round((Date.now() - new Date(iso + 'T00:00:00Z').getTime()) / 86400_000);
}

export function UsersAnalytics() {
  const [data, setData] = useState<UsersData | null>(null);
  const [holders, setHolders] = useState<HoldersData | null>(null);
  const [err, setErr] = useState<string | null>(null);

  const [view, setView] = useState<View>('leaderboard');
  const [range, setRange] = useState<Range>('all');
  const [growthMetric, setGrowthMetric] = useState<'cumulative' | 'holders'>('holders');
  const [sortKey, setSortKey] = useState<SortKey>('holdingUsd');
  const [sortAsc, setSortAsc] = useState<boolean>(false);
  const [search, setSearch] = useState('');
  const [page, setPage] = useState(0);
  const [leg, setLeg] = useState<LegFilter>('ALL');
  const [conStatus, setConStatus] = useState<StatusFilter>('active');
  const [hidePools, setHidePools] = useState<boolean>(true);
  const PAGE = 50;

  useEffect(() => {
    fetch('/users.json').then(r => r.ok ? r.json() : Promise.reject(`HTTP ${r.status}`)).then(setData).catch(e => setErr(String(e)));
    fetch('/holders.json').then(r => r.ok ? r.json() : Promise.reject(`HTTP ${r.status}`)).then(setHolders).catch(() => null);
  }, []);

  const sortedWallets = useMemo(() => {
    if (!data) return [];
    let arr = [...data.wallets];
    if (hidePools) arr = arr.filter(w => !w.isPool);
    if (search) {
      const q = search.toLowerCase();
      arr = arr.filter(w => w.wallet.toLowerCase().includes(q));
    }
    arr.sort((a, b) => {
      let va: number, vb: number;
      switch (sortKey) {
        case 'holdingUsd':      va = a.holdingUsd;       vb = b.holdingUsd; break;
        case 'unclaimedUsd':    va = a.unclaimedUsd;     vb = b.unclaimedUsd; break;
        case 'nSwaps':          va = a.nSwaps;           vb = b.nSwaps; break;
        case 'lifetimeMarkets': va = a.lifetimeMarkets;  vb = b.lifetimeMarkets; break;
        case 'firstSeen':       va = a.firstSeen ? new Date(a.firstSeen).getTime() : 9e12;
                                vb = b.firstSeen ? new Date(b.firstSeen).getTime() : 9e12; break;
        case 'lastSeen':        va = a.lastSeen  ? new Date(a.lastSeen).getTime()  : 0;
                                vb = b.lastSeen  ? new Date(b.lastSeen).getTime()  : 0; break;
      }
      return (va - vb) * (sortAsc ? 1 : -1);
    });
    return arr;
  }, [data, sortKey, sortAsc, search]);

  const growthChartData = useMemo(() => {
    if (!data) return [];
    if (growthMetric === 'holders' && data.holdersGrowth) {
      const hg = data.holdersGrowth;
      const start = rangeStartIdx(hg.dates, range);
      return hg.dates.slice(start).map((d, i) => ({
        date: d, PT: hg.PT[start + i] || 0, YT: hg.YT[start + i] || 0, LP: hg.LP[start + i] || 0,
      }));
    }
    const start = rangeStartIdx(data.growth.dates, range);
    return data.growth.dates.slice(start).map((d, i) => ({
      date: d,
      Cumulative: data.growth.cumulativeWallets[start + i] || 0,
      NewWallets: data.growth.newWallets[start + i] || 0,
    }));
  }, [data, growthMetric, range]);

  const retentionChartData = useMemo(() => {
    if (!data) return [];
    const start = rangeStartIdx(data.retention.weeks, range);
    return data.retention.weeks.slice(start).map((w, i) => ({
      week: w, New: data.retention.new[start + i] || 0, Returning: data.retention.returning[start + i] || 0,
    }));
  }, [data, range]);

  // Per-market concentration (the old standalone Holders tab content).
  const concentrationRows = useMemo(() => {
    if (!holders) return [];
    return holders.rows.filter(r =>
      (leg === 'ALL' || r.leg === leg) &&
      (conStatus === 'all' || (conStatus === 'active' && r.status === 'active')) &&
      !r.isTest
    );
  }, [holders, leg, conStatus]);

  function onSort(k: SortKey) {
    if (k === sortKey) setSortAsc(v => !v);
    else { setSortKey(k); setSortAsc(k === 'firstSeen'); }
    setPage(0);
  }
  function arrow(k: SortKey) {
    if (k !== sortKey) return null;
    return <span className="ml-1 text-white/60">{sortAsc ? '↑' : '↓'}</span>;
  }

  if (err) return <div className="text-red-400 text-sm p-4">Failed to load users.json: {err}</div>;
  if (!data) return <div className="text-white/40 text-sm p-4">Loading users…</div>;

  const h = data.headline;
  const c = data.concentration30d;
  const totalPages = Math.ceil(sortedWallets.length / PAGE);

  return (
    <section className="rounded-2xl border border-white/10 bg-white/5 p-4 mb-6">
      <header className="flex items-baseline justify-between flex-wrap gap-2 mb-3">
        <h2 className="text-sm uppercase tracking-wider text-white/60">Users</h2>
        <div className="flex items-center gap-1 flex-wrap">
          {([
            { key: 'leaderboard',   label: 'Users' },
            { key: 'cohorts',       label: 'Cohorts' },
            { key: 'swaps',         label: 'Swaps' },
            { key: 'growth',        label: 'Growth' },
            { key: 'retention',     label: 'Retention' },
            { key: 'whales',        label: 'Whales' },
            { key: 'concentration', label: 'Concentration' },
          ] as { key: View; label: string }[]).map(v => (
            <button key={v.key} onClick={() => setView(v.key)}
              className={`text-xs px-3 py-1 rounded-lg border ${view === v.key ? 'border-white/30 bg-white/10 text-white' : 'border-white/10 text-white/40'}`}>
              {v.label}
            </button>
          ))}
          {(view === 'growth' || view === 'retention' || view === 'swaps') && (
            <>
              <span className="w-2" />
              {(['30d', '90d', '1y', 'all'] as Range[]).map(r => (
                <button key={r} onClick={() => setRange(r)}
                  className={`text-xs px-2.5 py-1 rounded-md ${range === r ? 'bg-white/10 text-white' : 'text-white/30 hover:text-white/60'}`}>
                  {r === 'all' ? 'All' : r.toUpperCase()}
                </button>
              ))}
            </>
          )}
        </div>
      </header>

      {/* ─── Leaderboard ─── */}
      {view === 'leaderboard' && (
        <>
          <div className="flex items-center justify-between gap-3 mb-3 flex-wrap">
            <input value={search} onChange={e => { setSearch(e.target.value); setPage(0); }}
              placeholder="Search wallet address…"
              className="flex-1 max-w-md bg-[#0a0a0a]/60 border border-white/10 focus:border-white/30 focus:outline-none rounded-md px-3 py-1.5 text-xs tabular-nums placeholder-white/20" />
            <label className="flex items-center gap-1.5 text-[11px] text-white/50 cursor-pointer select-none">
              <input type="checkbox" checked={hidePools} onChange={e => { setHidePools(e.target.checked); setPage(0); }}
                className="accent-white/40" />
              Hide pool PDAs ({data.wallets.filter(w => w.isPool).length})
            </label>
          </div>
          <div className="overflow-x-auto">
            <table className="w-full text-xs">
              <thead className="text-white/40 border-b border-white/10">
                <tr>
                  <th className="text-left py-2 font-normal">#</th>
                  <th className="text-left py-2 font-normal">Wallet</th>
                  <th className="text-right py-2 font-normal cursor-pointer select-none hover:text-white/70"
                      onClick={() => onSort('holdingUsd')}
                      title="Current value of PT+YT+LP positions in active markets">
                    Active Holdings {arrow('holdingUsd')}
                  </th>
                  <th className="text-right py-2 font-normal cursor-pointer select-none hover:text-white/70"
                      onClick={() => onSort('unclaimedUsd')}
                      title="Current YT USD for wallets that have not claimed yield (approx)">
                    Unclaimed {arrow('unclaimedUsd')}
                  </th>
                  <th className="text-right py-2 font-normal cursor-pointer select-none hover:text-white/70"
                      onClick={() => onSort('nSwaps')}>
                    Swaps {arrow('nSwaps')}
                  </th>
                  <th className="text-right py-2 font-normal cursor-pointer select-none hover:text-white/70"
                      onClick={() => onSort('lifetimeMarkets')}>
                    Markets {arrow('lifetimeMarkets')}
                  </th>
                  <th className="text-right py-2 font-normal cursor-pointer select-none hover:text-white/70"
                      onClick={() => onSort('firstSeen')}>
                    First {arrow('firstSeen')}
                  </th>
                  <th className="text-right py-2 font-normal cursor-pointer select-none hover:text-white/70"
                      onClick={() => onSort('lastSeen')}>
                    Last {arrow('lastSeen')}
                  </th>
                  <th className="text-left py-2 font-normal">Status</th>
                </tr>
              </thead>
              <tbody>
                {sortedWallets.slice(page * PAGE, (page + 1) * PAGE).map((w, i) => {
                  const rank = page * PAGE + i;
                  const ds = daysSinceUtc(w.lastSeen);
                  const isActive = ds !== null && ds <= 30;
                  return (
                    <tr key={w.wallet} className="border-b border-white/5 hover:bg-white/5">
                      <td className="py-1 text-white/30 tabular-nums">{rank + 1}</td>
                      <td className="py-1 tabular-nums">
                        <a href={`/wallet/?addr=${w.wallet}`} className="text-white/80 hover:text-white">
                          <span className="hidden xl:inline">{w.wallet}</span>
                          <span className="xl:hidden">{w.wallet.slice(0, 4)}…{w.wallet.slice(-4)}</span>
                        </a>
                        {w.isPool && (
                          <span className="ml-2 text-[9px] uppercase tracking-wider px-1 py-0.5 rounded border border-purple-400/30 bg-purple-400/10 text-purple-300"
                                title="Token-holding PDA — protocol vault, never signs swaps">
                            pool
                          </span>
                        )}
                        <a href={`https://solscan.io/account/${w.wallet}`} target="_blank" rel="noopener noreferrer"
                           className="ml-1 text-white/20 hover:text-white/60">↗</a>
                      </td>
                      <td className="py-1 text-right tabular-nums text-emerald-400/80">{fmtUsd(w.holdingUsd)}</td>
                      <td className="py-1 text-right tabular-nums text-yellow-400/70">{w.unclaimedUsd > 0 ? fmtUsd(w.unclaimedUsd) : <span className="text-white/15">–</span>}</td>
                      <td className="py-1 text-right tabular-nums text-white/60">{w.nSwaps || <span className="text-white/15">–</span>}</td>
                      <td className="py-1 text-right tabular-nums text-white/60">{w.lifetimeMarkets || <span className="text-white/15">–</span>}</td>
                      <td className="py-1 text-right tabular-nums text-white/30">{w.firstSeen || '–'}</td>
                      <td className="py-1 text-right tabular-nums text-white/30">{w.lastSeen || '–'}</td>
                      <td className="py-1">
                        {ds !== null
                          ? <span className={`text-[10px] px-1.5 py-0.5 rounded ${isActive ? 'bg-emerald-500/10 text-emerald-400' : 'bg-white/5 text-white/30'}`}>
                              {isActive ? 'Active' : `${ds}d ago`}
                            </span>
                          : <span className="text-white/15 text-[10px]">–</span>}
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
          {sortedWallets.length > PAGE && (
            <div className="flex items-center justify-between mt-3 text-xs text-white/40">
              <span>{search ? `${sortedWallets.length.toLocaleString()} results` : `${sortedWallets.length.toLocaleString()} wallets shown (top 2000)`} • Page {page + 1} of {totalPages}</span>
              <div className="flex gap-1">
                <button onClick={() => setPage(0)} disabled={page === 0}
                  className="px-2 py-0.5 rounded border border-white/10 hover:text-white disabled:opacity-20">First</button>
                <button onClick={() => setPage(p => Math.max(0, p - 1))} disabled={page === 0}
                  className="px-2 py-0.5 rounded border border-white/10 hover:text-white disabled:opacity-20">Prev</button>
                <button onClick={() => setPage(p => Math.min(totalPages - 1, p + 1))} disabled={page >= totalPages - 1}
                  className="px-2 py-0.5 rounded border border-white/10 hover:text-white disabled:opacity-20">Next</button>
                <button onClick={() => setPage(totalPages - 1)} disabled={page >= totalPages - 1}
                  className="px-2 py-0.5 rounded border border-white/10 hover:text-white disabled:opacity-20">Last</button>
              </div>
            </div>
          )}
        </>
      )}

      {/* ─── Swaps (lifetime trading activity cohort + historical) ─── */}
      {view === 'swaps' && (() => {
        const start = rangeStartIdx(data.growth.dates, range);
        const series = data.growth.dates.slice(start).map((d, i) => ({
          date: d,
          'Active wallets':  data.growth.activeWallets[start + i] || 0,
          'New wallets':     data.growth.newWallets[start + i] || 0,
          Swaps:             data.growth.swaps[start + i] || 0,
          'Volume USD':      data.growth.volumeUsd[start + i] || 0,
        }));
        return (
          <>
            <div className="grid grid-cols-2 sm:grid-cols-5 gap-2 mb-4 text-xs">
              {[
                { label: '1 swap',          value: h.oneSwap,             sub: 'one-and-done' },
                { label: '2–9 swaps',       value: h.casualWallets,       sub: 'casual' },
                { label: '10–99 swaps',     value: h.activeWallets,       sub: 'active' },
                { label: '100+ swaps',      value: h.powerWallets,        sub: 'power users' },
                { label: 'max by 1 wallet', value: h.maxSwapsByOneWallet, sub: 'top trader' },
              ].map(s => (
                <div key={s.label} className="rounded-lg border border-white/10 p-2">
                  <div className="text-[10px] uppercase text-white/40">{s.label}</div>
                  <div className="text-base font-semibold tabular-nums text-white mt-0.5">{s.value.toLocaleString()}</div>
                  <div className="text-[10px] text-white/30">{s.sub}</div>
                </div>
              ))}
            </div>
            <ResponsiveContainer width="100%" height={260}>
              <ComposedChart data={series as any} margin={{ top: 10, right: 50, left: 0, bottom: 0 }}>
                <XAxis dataKey="date" tick={{ fill: '#888', fontSize: 11 }} />
                <YAxis yAxisId="L" tick={{ fill: '#888', fontSize: 11 }} tickFormatter={n => fmt(n)} />
                <YAxis yAxisId="R" orientation="right" tick={{ fill: '#fbbf24', fontSize: 11 }} tickFormatter={n => `$${fmt(n)}`} width={56} />
                <Tooltip contentStyle={{ background: '#0a0a0a', border: '1px solid rgba(255,255,255,0.15)', fontSize: 11 }}
                  formatter={(v: any, name: any) => name === 'Volume USD' ? `$${fmt(Number(v))}` : fmt(Number(v))} />
                <Bar yAxisId="L" dataKey="Swaps"           fill="#38bdf8" fillOpacity={0.55} isAnimationActive={false} />
                <Line yAxisId="L" type="monotone" dataKey="Active wallets" stroke="#a78bfa" strokeWidth={1.5} dot={false} isAnimationActive={false} />
                <Line yAxisId="L" type="monotone" dataKey="New wallets"    stroke="#4ade80" strokeWidth={1.5} dot={false} isAnimationActive={false} />
                <Line yAxisId="R" type="monotone" dataKey="Volume USD"     stroke="#fbbf24" strokeWidth={1.5} strokeOpacity={0.7} dot={false} isAnimationActive={false} />
              </ComposedChart>
            </ResponsiveContainer>
            <div className="text-[10px] text-white/30 mt-1">
              Daily Swaps (bars, left axis) · Active wallets (violet) · New wallets (green) · Volume USD (amber, right axis).
              Lifetime volume = ${fmt(h.lifetimeVolumeUsd)}; recent 30d concentration: top-10 = {c.top10SharePct.toFixed(0)}% of volume, top-100 = {c.top100SharePct.toFixed(0)}%.
            </div>
          </>
        );
      })()}

      {/* ─── Cohorts ─── */}
      {view === 'cohorts' && (() => {
        const COHORT_META: Record<string, { label: string; range: string; color: string }> = {
          whale:  { label: 'Whale',  range: '≥ $100K',    color: '#a78bfa' },
          large:  { label: 'Large',  range: '$10K – $100K', color: '#38bdf8' },
          medium: { label: 'Medium', range: '$1K – $10K',  color: '#4ade80' },
          small:  { label: 'Small',  range: '$100 – $1K',  color: '#fb923c' },
          micro:  { label: 'Micro',  range: '$0 – $100',   color: '#f87171' },
        };
        const order = ['whale', 'large', 'medium', 'small', 'micro'];
        const cohorts = data.cohorts.filter(c => c.cohort !== 'zero').sort((a, b) => order.indexOf(a.cohort) - order.indexOf(b.cohort));
        const totalActive = cohorts.reduce((s, c) => s + c.count, 0);
        const totalUsd = cohorts.reduce((s, c) => s + c.totalHoldingsUsd, 0);
        return (
          <div className="overflow-x-auto">
            <div className="text-xs text-white/40 mb-2">{totalActive.toLocaleString()} wallets with active positions • {fmtUsd(totalUsd)} total</div>
            <table className="w-full text-xs">
              <thead className="text-white/40 border-b border-white/10">
                <tr>
                  <th className="text-left py-2 font-normal">Cohort</th>
                  <th className="text-left py-2 font-normal">Range</th>
                  <th className="text-right py-2 font-normal">Wallets</th>
                  <th className="text-right py-2 font-normal">% Wallets</th>
                  <th className="text-right py-2 font-normal">Total Holdings</th>
                  <th className="text-right py-2 font-normal">% TVL</th>
                  <th className="text-right py-2 font-normal">Trades</th>
                  <th className="text-right py-2 font-normal">Avg Swaps</th>
                  <th className="text-right py-2 font-normal">Avg Markets</th>
                  <th className="text-right py-2 font-normal">Active (30d)</th>
                </tr>
              </thead>
              <tbody>
                {cohorts.map(c => {
                  const meta = COHORT_META[c.cohort] || { label: c.cohort, range: '', color: '#888' };
                  const pctWallets = totalActive ? (c.count / totalActive) * 100 : 0;
                  const pctUsd = totalUsd ? (c.totalHoldingsUsd / totalUsd) * 100 : 0;
                  const activePct = c.count ? (c.active30d / c.count) * 100 : 0;
                  return (
                    <tr key={c.cohort} className="border-b border-white/5">
                      <td className="py-1.5 font-semibold" style={{ color: meta.color }}>{meta.label}</td>
                      <td className="py-1.5 text-white/40">{meta.range}</td>
                      <td className="py-1.5 text-right tabular-nums text-white">{c.count.toLocaleString()}</td>
                      <td className="py-1.5 text-right tabular-nums text-white/50">{pctWallets.toFixed(1)}%</td>
                      <td className="py-1.5 text-right tabular-nums text-emerald-400/80">{fmtUsd(c.totalHoldingsUsd)}</td>
                      <td className="py-1.5 text-right tabular-nums text-white/50">{pctUsd.toFixed(1)}%</td>
                      <td className="py-1.5 text-right tabular-nums text-white/50">{c.trades.toLocaleString()}</td>
                      <td className="py-1.5 text-right tabular-nums text-white/50">{c.avgSwaps.toFixed(0)}</td>
                      <td className="py-1.5 text-right tabular-nums text-white/50">{c.avgMarkets.toFixed(1)}</td>
                      <td className="py-1.5 text-right tabular-nums text-white/50">
                        <span className="inline-flex items-center gap-2">
                          <span className="w-10 h-1 bg-white/10 rounded-full overflow-hidden">
                            <span className="block h-full bg-emerald-400" style={{ width: `${activePct}%` }} />
                          </span>
                          {activePct.toFixed(0)}%
                        </span>
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
        );
      })()}

      {/* ─── Growth ─── */}
      {view === 'growth' && (
        <>
          <div className="flex items-center gap-1 mb-2">
            {(['holders', 'cumulative'] as const).map(m => (
              <button key={m} onClick={() => setGrowthMetric(m)}
                className={`text-[11px] px-2.5 py-1 rounded-md border ${growthMetric === m ? 'border-white/30 bg-white/10 text-white' : 'border-white/10 text-white/40'}`}>
                {m === 'holders' ? 'PT/YT/LP holders' : 'Cumulative wallets'}
              </button>
            ))}
          </div>
          <ResponsiveContainer width="100%" height={260}>
            {growthMetric === 'holders' ? (
              <ComposedChart data={growthChartData as any} margin={{ top: 10, right: 10, left: 0, bottom: 0 }}>
                <XAxis dataKey="date" tick={{ fill: '#888', fontSize: 11 }} />
                <YAxis tick={{ fill: '#888', fontSize: 11 }} tickFormatter={n => fmt(n)} />
                <Tooltip contentStyle={{ background: '#0a0a0a', border: '1px solid rgba(255,255,255,0.15)', fontSize: 11 }} formatter={(v: any) => fmt(Number(v))} />
                <Line type="monotone" dataKey="PT" stroke="#a78bfa" strokeWidth={1.5} dot={false} isAnimationActive={false} />
                <Line type="monotone" dataKey="YT" stroke="#38bdf8" strokeWidth={1.5} dot={false} isAnimationActive={false} />
                <Line type="monotone" dataKey="LP" stroke="#4ade80" strokeWidth={1.5} dot={false} isAnimationActive={false} />
              </ComposedChart>
            ) : (
              <AreaChart data={growthChartData as any} margin={{ top: 10, right: 10, left: 0, bottom: 0 }}>
                <XAxis dataKey="date" tick={{ fill: '#888', fontSize: 11 }} />
                <YAxis tick={{ fill: '#888', fontSize: 11 }} tickFormatter={n => fmt(n)} />
                <Tooltip contentStyle={{ background: '#0a0a0a', border: '1px solid rgba(255,255,255,0.15)', fontSize: 11 }} formatter={(v: any) => fmt(Number(v))} />
                <Area type="monotone" dataKey="Cumulative" stroke="#a78bfa" fill="#a78bfa66" />
              </AreaChart>
            )}
          </ResponsiveContainer>
          {growthMetric === 'holders' && (
            <div className="text-[10px] text-white/30 mt-1">
              Per-leg active holders (markets unmatured as of each date). PT reconstructed from SPL transfers; YT/LP from Anchor instruction logs. Today anchored to on-chain snapshot.
            </div>
          )}
        </>
      )}

      {/* ─── Retention ─── */}
      {view === 'retention' && (
        <>
          <ResponsiveContainer width="100%" height={260}>
            <BarChart data={retentionChartData as any} margin={{ top: 10, right: 10, left: 0, bottom: 0 }}>
              <XAxis dataKey="week" tick={{ fill: '#888', fontSize: 11 }} interval={Math.max(1, Math.floor(retentionChartData.length / 10))}
                tickFormatter={d => { const dt = new Date(d + 'T00:00:00Z'); return `${dt.toLocaleString('en', { month: 'short' })} ${dt.getUTCDate()}`; }} />
              <YAxis tick={{ fill: '#888', fontSize: 11 }} tickFormatter={n => fmt(n)} />
              <Tooltip contentStyle={{ background: '#0a0a0a', border: '1px solid rgba(255,255,255,0.15)', fontSize: 11 }}
                labelFormatter={d => {
                  const mon = new Date(d + 'T00:00:00Z');
                  const sun = new Date(mon.getTime() + 6 * 86400000);
                  return `Week of ${mon.toLocaleDateString('en', { month: 'short', day: 'numeric' })} – ${sun.toLocaleDateString('en', { month: 'short', day: 'numeric', year: 'numeric' })}`;
                }} />
              <Bar dataKey="New" stackId="r" fill="#4ade80" fillOpacity={0.85} isAnimationActive={false} />
              <Bar dataKey="Returning" stackId="r" fill="#6b66ff" fillOpacity={0.85} isAnimationActive={false} />
            </BarChart>
          </ResponsiveContainer>
          <div className="flex gap-4 justify-center mt-2 text-[11px] text-white/50">
            <span className="flex items-center gap-1"><span className="w-2.5 h-2.5 rounded-full bg-emerald-400" /> New</span>
            <span className="flex items-center gap-1"><span className="w-2.5 h-2.5 rounded-full" style={{ background: '#6b66ff' }} /> Returning</span>
          </div>
        </>
      )}

      {/* ─── Whale Activity ─── */}
      {view === 'whales' && (
        <div className="overflow-x-auto">
          <table className="w-full text-xs">
            <thead className="text-white/40 border-b border-white/10">
              <tr>
                <th className="text-left py-2 font-normal">#</th>
                <th className="text-left py-2 font-normal">Date</th>
                <th className="text-left py-2 font-normal">Wallet</th>
                <th className="text-left py-2 font-normal">Market</th>
                <th className="text-left py-2 font-normal">Platform</th>
                <th className="text-left py-2 font-normal">Action</th>
                <th className="text-right py-2 font-normal">USD</th>
              </tr>
            </thead>
            <tbody>
              {data.whaleEvents.map((w, i) => (
                <tr key={w.signature} className="border-b border-white/5 hover:bg-white/5">
                  <td className="py-1 text-white/30 tabular-nums">{i + 1}</td>
                  <td className="py-1 text-white/50">{w.date}</td>
                  <td className="py-1 tabular-nums">
                    <a href={`/wallet/?addr=${w.signer}`} className="text-white/80 hover:text-white">
                      <span className="hidden xl:inline">{w.signer}</span>
                      <span className="xl:hidden">{w.signer.slice(0, 4)}…{w.signer.slice(-4)}</span>
                    </a>
                  </td>
                  <td className="py-1">
                    <a href={`/market/?key=${w.market}`} className="text-white/70 hover:text-white">{w.market}</a>
                  </td>
                  <td className="py-1 text-white/50">{w.platform}</td>
                  <td className="py-1 text-white/50">{w.action}{w.direction ? ` (${w.direction})` : ''}</td>
                  <td className="py-1 text-right tabular-nums text-emerald-400/80">{fmtUsd(w.usd)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}

      {/* ─── Concentration (per-market top-N) ─── */}
      {view === 'concentration' && (
        <>
          <div className="flex items-center gap-1 mb-3 flex-wrap">
            {(['ALL', 'PT', 'YT', 'LP'] as LegFilter[]).map(l => (
              <button key={l} onClick={() => setLeg(l)}
                className={`text-[11px] px-2.5 py-1 rounded-md border ${leg === l ? 'border-white/30 bg-white/10 text-white' : 'border-white/10 text-white/40'}`}>
                {l}
              </button>
            ))}
            <span className="w-2" />
            {(['active', 'all'] as StatusFilter[]).map(s => (
              <button key={s} onClick={() => setConStatus(s)}
                className={`text-[11px] px-2.5 py-1 rounded-md border ${conStatus === s ? 'border-white/30 bg-white/10 text-white' : 'border-white/10 text-white/40'}`}>
                {s === 'active' ? 'Active' : 'All'}
              </button>
            ))}
          </div>
          {!holders && <div className="text-white/40 text-xs">Loading concentration…</div>}
          {holders && (
            <div className="overflow-x-auto">
              <table className="w-full text-xs">
                <thead className="text-white/40 border-b border-white/10">
                  <tr>
                    <th className="text-left py-2 font-normal">Market</th>
                    <th className="text-left py-2 font-normal">Platform</th>
                    <th className="text-left py-2 font-normal">Leg</th>
                    <th className="text-right py-2 font-normal">Holders</th>
                    <th className="text-right py-2 font-normal">Top 1</th>
                    <th className="text-right py-2 font-normal">Top 5</th>
                    <th className="text-right py-2 font-normal">Top 10</th>
                    <th className="text-right py-2 font-normal">Supply</th>
                  </tr>
                </thead>
                <tbody>
                  {concentrationRows.map(r => (
                    <tr key={`${r.marketKey}-${r.leg}`} className="border-b border-white/5">
                      <td className="py-1.5 text-white/85">
                        <a href={`/market/?key=${r.marketKey}`} className="hover:text-white">{r.marketKey}</a>
                        {r.status !== 'active' && <span className="ml-1 text-white/30">·exp</span>}
                      </td>
                      <td className="py-1.5 text-white/60">{platformOfTicker(r.ticker)}</td>
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
          )}
        </>
      )}
    </section>
  );
}
