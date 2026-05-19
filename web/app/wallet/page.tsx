'use client';
import { Suspense, useEffect, useMemo, useState } from 'react';
import Link from 'next/link';
import { useSearchParams } from 'next/navigation';

type TokenChange = { symbol: string; delta: number; usd: number | null };
type WalletEvent = {
  sig: string;
  blockTime: number;
  action: string;
  market: string | null;
  ticker: string | null;
  usd: number | null;
  changes: TokenChange[];
};

type Filter = 'buyYt' | 'sellYt' | 'tradePt' | 'claimYield' | 'addLiq' | 'removeLiq' | 'strip' | 'merge' | 'redeemPt' | 'deposit' | 'withdraw';
const ALL_FILTERS: Filter[] = ['buyYt', 'sellYt', 'tradePt', 'claimYield', 'addLiq', 'removeLiq', 'strip', 'merge', 'redeemPt', 'deposit', 'withdraw'];
const LABEL: Record<string, string> = {
  buyYt:'Buy YT', sellYt:'Sell YT', tradePt:'Trade PT',
  claimYield:'Claim Yield', addLiq:'Add Liquidity', removeLiq:'Remove Liquidity',
  strip:'Strip', merge:'Merge', redeemPt:'Redeem PT',
  deposit:'Deposit', withdraw:'Withdraw',
};
const COLOR: Record<string, string> = {
  buyYt:'text-emerald-400', sellYt:'text-rose-400', tradePt:'text-sky-400',
  claimYield:'text-yellow-400', addLiq:'text-purple-400', removeLiq:'text-purple-400/70',
  strip:'text-white/50', merge:'text-white/50', redeemPt:'text-white/60',
  deposit:'text-cyan-400', withdraw:'text-orange-400/70',
};
type SortKey = 'date' | 'market' | 'action' | 'usd';

function fmtUsd(n: number) {
  if (!n) return '–';
  if (n >= 1e9) return `$${(n / 1e9).toFixed(2)}B`;
  if (n >= 1e6) return `$${(n / 1e6).toFixed(2)}M`;
  if (n >= 1e3) return `$${(n / 1e3).toFixed(1)}K`;
  return `$${n.toFixed(2)}`;
}
function fmtAmount(n: number) {
  const abs = Math.abs(n);
  if (abs >= 1e9) return `${(n / 1e9).toFixed(2)}B`;
  if (abs >= 1e6) return `${(n / 1e6).toFixed(2)}M`;
  if (abs >= 1e3) return `${(n / 1e3).toFixed(1)}K`;
  if (abs >= 1) return n.toFixed(2);
  return n.toFixed(4);
}

function WalletDetailView() {
  const sp = useSearchParams();
  const addr = sp.get('addr') || '';

  const [events, setEvents] = useState<WalletEvent[] | null>(null);
  const [notFound, setNotFound] = useState(false);
  const [unclaimed, setUnclaimed] = useState<{ marketKey: string; ytBalance: number }[]>([]);
  const [enabled, setEnabled] = useState<Set<Filter>>(new Set(ALL_FILTERS));
  const [sortKey, setSortKey] = useState<SortKey>('date');
  const [asc, setAsc] = useState(false);

  useEffect(() => {
    fetch(`/wallet/${addr}.json`)
      .then(r => {
        if (r.status === 404) { setNotFound(true); setEvents([]); return null; }
        return r.json();
      })
      .then(d => { if (d) setEvents(d.events); })
      .catch(() => setEvents([]));
    fetch('/unclaimed_yield.json')
      .then(r => r.json())
      .then(d => setUnclaimed(d.byWallet?.[addr] ?? []))
      .catch(() => null);
  }, [addr]);

  const actionCounts = useMemo(() => {
    const counts: Record<string, number> = {};
    for (const e of events ?? []) if (e.action) counts[e.action] = (counts[e.action] || 0) + 1;
    return counts;
  }, [events]);

  const visible = useMemo(() => {
    let arr = (events ?? []).filter(e => e.action && enabled.has(e.action as Filter));
    arr.sort((a, b) => {
      let c = 0;
      if (sortKey === 'date') c = (a.blockTime || 0) - (b.blockTime || 0);
      else if (sortKey === 'market') c = (a.market || '').localeCompare(b.market || '');
      else if (sortKey === 'action') c = (a.action || '').localeCompare(b.action || '');
      else if (sortKey === 'usd') c = (a.usd || 0) - (b.usd || 0);
      return c * (asc ? 1 : -1);
    });
    return arr;
  }, [events, enabled, sortKey, asc]);

  function onSort(k: SortKey) { if (sortKey === k) setAsc(v => !v); else { setSortKey(k); setAsc(k === 'date'); } }
  function arrow(k: SortKey) { return sortKey === k ? <span className="ml-1 text-white/70">{asc ? '↑' : '↓'}</span> : null; }
  function toggle(f: Filter) { setEnabled(prev => { const n = new Set(prev); if (n.has(f)) n.delete(f); else n.add(f); return n; }); }
  const allOn = enabled.size === ALL_FILTERS.length;

  const totalEvents = (events ?? []).filter(e => e.action).length;
  const markets = new Set((events ?? []).map(e => e.market).filter(Boolean));
  const totalUsdValue = (events ?? []).reduce((s, e) => s + (e.usd || 0), 0);

  return (
    <main className="mx-auto max-w-[1400px] px-4 sm:px-6 py-10">
      <Link href="/" className="text-white/40 hover:text-white text-sm">← back</Link>
      <h1 className="mt-4 text-2xl font-semibold text-white">Wallet Activity</h1>
      <p className="font-mono text-xs text-white/50 break-all mt-1 flex items-center gap-3 flex-wrap">
        <span>{addr}</span>
        <a href={`https://solscan.io/account/${addr}`} target="_blank" rel="noopener noreferrer" className="text-white/40 hover:text-white">solscan ↗</a>
      </p>

      {/* Summary stats */}
      <div className="mt-6 grid grid-cols-2 md:grid-cols-4 lg:grid-cols-6 gap-3 text-sm">
        <Stat label="Total Events" value={String(totalEvents)} />
        <Stat label="Markets" value={String(markets.size)} />
        <Stat label="Lifetime Volume" value={fmtUsd(totalUsdValue)} />
        {ALL_FILTERS.filter(f => actionCounts[f]).map(f => (
          <Stat key={f} label={LABEL[f]} value={String(actionCounts[f])} />
        ))}
      </div>

      {/* Unclaimed yield */}
      {unclaimed.length > 0 && (
        <div className="mt-4 rounded-xl border border-rose-500/20 bg-rose-500/5 px-4 py-3">
          <div className="text-xs text-rose-400 font-semibold mb-1">Unclaimed yield in:</div>
          <div className="flex flex-wrap gap-2">
            {unclaimed.map(u => (
              <Link key={u.marketKey} href={`/market/?key=${u.marketKey}`}
                    className="text-xs px-2 py-1 rounded bg-rose-500/10 text-rose-300 hover:bg-rose-500/20">
                {u.marketKey}
              </Link>
            ))}
          </div>
        </div>
      )}

      {notFound && (
        <div className="mt-4 rounded-xl border border-white/10 bg-white/5 px-4 py-3 text-sm text-white/60">
          No timeline data for this wallet. Wallets with fewer than 3 indexed events are not sharded.{' '}
          <a href={`https://solscan.io/account/${addr}`} target="_blank" rel="noopener noreferrer" className="text-white/80 hover:text-white">View on Solscan ↗</a>
        </div>
      )}

      {/* Filter chips */}
      {events && events.length > 0 && (
        <div className="mt-6 flex flex-wrap items-center gap-2">
          <button onClick={() => setEnabled(new Set(allOn ? [] : ALL_FILTERS))}
            className={`text-xs px-3 py-1.5 rounded-lg border ${allOn ? 'border-white/30 bg-white/10 text-white' : 'border-white/10 text-white/40'}`}>
            {allOn ? 'All' : `${enabled.size} selected`}
          </button>
          {ALL_FILTERS.filter(f => actionCounts[f]).map(f => (
            <button key={f} onClick={() => toggle(f)}
              className={`text-xs px-3 py-1.5 rounded-lg border ${enabled.has(f) ? 'border-white/30 bg-white/10 text-white' : 'border-white/10 text-white/40'}`}>
              {LABEL[f]} ({actionCounts[f]})
            </button>
          ))}
        </div>
      )}

      {/* Events table */}
      {events && events.length > 0 && (
        <section className="mt-4 rounded-2xl border border-white/10 bg-white/5 overflow-x-auto">
          <table className="w-full text-sm">
            <thead className="text-xs uppercase tracking-wider text-white/40">
              <tr>
                <th className="px-4 py-2 text-left cursor-pointer" onClick={() => onSort('date')}>Date{arrow('date')}</th>
                <th className="px-4 py-2 text-left cursor-pointer" onClick={() => onSort('market')}>Market{arrow('market')}</th>
                <th className="px-4 py-2 text-left cursor-pointer" onClick={() => onSort('action')}>Action{arrow('action')}</th>
                <th className="px-4 py-2 text-right cursor-pointer" onClick={() => onSort('usd')}>USD{arrow('usd')}</th>
                <th className="px-4 py-2 text-left">Token Changes</th>
                <th className="px-4 py-2">Tx</th>
              </tr>
            </thead>
            <tbody className="divide-y divide-white/5 text-[13px]">
              {visible.length === 0 && (
                <tr><td className="px-4 py-3 text-white/30" colSpan={6}>No events match filters.</td></tr>
              )}
              {visible.map((e, i) => (
                <tr key={`${e.sig}-${i}`} className="hover:bg-white/5">
                  <td className="px-4 py-1.5 text-white/50 font-mono text-xs whitespace-nowrap">
                    {new Date(e.blockTime * 1000).toISOString().replace('T', ' ').slice(0, 19)}
                  </td>
                  <td className="px-4 py-1.5 text-white/70">
                    {e.market ? (
                      <Link href={`/market/?key=${e.market}`} className="hover:text-white">{e.market}</Link>
                    ) : '–'}
                  </td>
                  <td className="px-4 py-1.5">
                    <span className={COLOR[e.action] || 'text-white/50'}>{LABEL[e.action] || e.action}</span>
                  </td>
                  <td className="px-4 py-1.5 text-right tabular-nums text-white/60">
                    {e.usd ? fmtUsd(e.usd) : '–'}
                  </td>
                  <td className="px-4 py-1.5 text-xs tabular-nums">
                    {e.changes?.length ? (
                      <div className="flex flex-col gap-0.5">
                        {e.changes.map((c, j) => (
                          <span key={j} className={c.delta > 0 ? 'text-emerald-400/80' : 'text-rose-400/80'}>
                            {c.delta > 0 ? '+' : ''}{fmtAmount(c.delta)} <span className="text-white/40">{c.symbol}</span>
                            {c.usd != null && Math.abs(c.usd) > 0.01 ? <span className="text-white/30 ml-1">({fmtUsd(Math.abs(c.usd))})</span> : null}
                          </span>
                        ))}
                      </div>
                    ) : '–'}
                  </td>
                  <td className="px-4 py-1.5">
                    <a href={`https://solscan.io/tx/${e.sig}`} target="_blank" rel="noopener noreferrer" className="text-white/30 hover:text-white">↗</a>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </section>
      )}
      {visible.length > 0 && (
        <div className="text-xs text-white/30 mt-2 text-center">
          Showing {visible.length} of {totalEvents} events
        </div>
      )}
    </main>
  );
}

function Stat({ label, value }: { label: string; value: string }) {
  return (
    <div className="rounded-xl border border-white/10 bg-white/5 px-3 py-2">
      <div className="text-[10px] uppercase tracking-wider text-white/40">{label}</div>
      <div className="mt-0.5 text-base font-semibold text-white tabular-nums">{value}</div>
    </div>
  );
}

export default function WalletPage() {
  return (
    <Suspense fallback={<div className="mx-auto max-w-[1400px] px-4 py-10 text-white/50">Loading…</div>}>
      <WalletDetailView />
    </Suspense>
  );
}
