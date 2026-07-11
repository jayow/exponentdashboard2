'use client';
import { Suspense, useEffect, useMemo, useState } from 'react';
import Link from 'next/link';
import { BackLink } from '@/components/BackLink';
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
// Semantic color rule: add/buy = green (emerald), remove/sell = red (rose).
// Neutral/ambiguous actions (tradePt, strip, merge, redeemPt) stay muted;
// claimYield keeps its yellow accent since it's its own category.
const COLOR: Record<string, string> = {
  buyYt:'text-emerald-400', sellYt:'text-rose-400', tradePt:'text-sky-400',
  claimYield:'text-yellow-400', addLiq:'text-emerald-400', removeLiq:'text-rose-400',
  strip:'text-white/50', merge:'text-white/50', redeemPt:'text-white/60',
  deposit:'text-emerald-400', withdraw:'text-rose-400',
};
type SortKey = 'date' | 'market' | 'action' | 'usd';

function fmtUsd(n: number) {
  if (!n) return '–';
  if (n >= 1e9) return `$${(n / 1e9).toFixed(2)}B`;
  if (n >= 1e6) return `$${(n / 1e6).toFixed(2)}M`;
  if (n >= 1e3) return `$${(n / 1e3).toFixed(1)}K`;
  return `$${n.toFixed(2)}`;
}
// Apple-style identifier truncation: first 6 + ellipsis + last 6.
// Long enough to disambiguate visually, short enough to read as a name.
function shortenAddr(a: string) {
  if (a.length <= 14) return a;
  return `${a.slice(0, 6)}…${a.slice(-6)}`;
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
  const [unclaimed, setUnclaimed] = useState<{
    marketKey: string;
    ytBalance: number;
    stagedUnderlying?: number;
    unstagedUnderlying?: number;
    unclaimedUnderlying?: number;
    unclaimedUsd?: number;
  }[]>([]);
  const [positions, setPositions] = useState<{ marketKey: string; ticker: string; maturityDate: string; leg: 'PT' | 'YT' | 'LP'; balance: number; usdValue?: number }[]>([]);
  const [enabled, setEnabled] = useState<Set<Filter>>(new Set(ALL_FILTERS));
  const [copied, setCopied] = useState(false);
  const copyAddr = () => {
    navigator.clipboard.writeText(addr).then(() => {
      setCopied(true);
      setTimeout(() => setCopied(false), 1600);
    });
  };
  const [sortKey, setSortKey] = useState<SortKey>('date');
  const [asc, setAsc] = useState(false);

  useEffect(() => {
    fetch(`/wallet/${addr}.json`)
      .then(r => {
        if (r.status === 404) { setNotFound(true); setEvents([]); return null; }
        return r.json();
      })
      .then(d => { if (d) { setEvents(d.events); setPositions(d.positions ?? []); } })
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
      <BackLink label="← Markets" className="text-[12px] text-white/30 hover:text-white/60" />

      {/* Header — address (identity) on left, hero stats inline on right.
          Items align on the baseline so the 44px headline and 22px stat
          values sit on the same typographic floor (Apple Stocks pattern). */}
      <header className="mt-8 flex items-baseline justify-between gap-x-10 gap-y-6 flex-wrap">
        <div className="flex items-baseline gap-3 shrink-0">
          <a href={`https://jup.ag/portfolio/${addr}`} target="_blank" rel="noopener noreferrer"
             className="text-[44px] leading-none font-medium tracking-tight tabular-nums text-white hover:text-white/70 transition"
             title={`${addr}\n\nOpen in Jupiter Portfolio`}>
            {shortenAddr(addr)}
          </a>
          <CopyIcon onClick={copyAddr} copied={copied} />
        </div>
        <div className="flex items-baseline gap-6 min-w-0">
          <Stat label="Lifetime Volume" value={fmtUsd(totalUsdValue)} />
          <Stat label="Markets" value={String(markets.size)} />
        </div>
      </header>

      {/* Current positions */}
      {positions.length > 0 && (
        <div className="mt-6">
          <h2 className="text-[11px] uppercase tracking-[0.18em] text-white/35 mb-3">Current positions</h2>
          <div className="border-t border-b border-white/[0.06] overflow-x-auto">
            <table className="w-full text-sm">
              <thead className="text-[11px] uppercase tracking-[0.18em] text-white/40 border-b border-white/[0.06]">
                <tr>
                  <th className="text-left px-4 py-2 font-normal">Market</th>
                  <th className="text-left px-4 py-2 font-normal">Leg</th>
                  <th className="text-left px-4 py-2 font-normal">Matures</th>
                  <th className="text-right px-4 py-2 font-normal">Balance</th>
                </tr>
              </thead>
              <tbody>
                {positions.map(p => (
                  <tr key={`${p.marketKey}:${p.leg}`} className="border-b border-white/[0.06] hover:bg-white/5">
                    <td className="px-4 py-1.5">
                      <Link href={`/market/?key=${p.marketKey}`} className="text-white/85 hover:text-white">{p.marketKey}</Link>
                    </td>
                    <td className="px-4 py-1.5 text-white/70">{p.leg}</td>
                    <td className="px-4 py-1.5 text-white/50">{p.maturityDate}</td>
                    <td className="px-4 py-1.5 text-right tabular-nums text-white">
                      {fmtAmount(p.balance)} {p.ticker}
                      {p.usdValue != null && (
                        <span className="text-white/40 ml-1.5">· {fmtUsd(p.usdValue)}</span>
                      )}
                    </td>
                  </tr>
                ))}
                {/* Unclaimed yield — one row per market with claim ready.
                    Balance shows total unclaimed in underlying tokens (staged +
                    unstaged); tooltip carries the USD value and the split. */}
                {unclaimed.map(u => {
                  const ytPos = positions.find(p => p.leg === 'YT' && p.marketKey === u.marketKey);
                  const amount = u.unclaimedUnderlying ?? 0;
                  const usd = u.unclaimedUsd ?? 0;
                  const staged = u.stagedUnderlying ?? 0;
                  const unstaged = u.unstagedUnderlying ?? 0;
                  const tip = `Unclaimed yield — $${usd.toFixed(2)}\n  staged   ${staged.toFixed(6)} ${ytPos?.ticker ?? ''}\n  unstaged ${unstaged.toFixed(6)} ${ytPos?.ticker ?? ''}`;
                  return (
                    <tr key={`unclaimed:${u.marketKey}`} className="border-b border-white/[0.06] hover:bg-white/5">
                      <td className="px-4 py-1.5">
                        <Link href={`/market/?key=${u.marketKey}`} className="text-white/85 hover:text-white">{u.marketKey}</Link>
                      </td>
                      <td className="px-4 py-1.5 text-rose-400/80">Unclaimed</td>
                      <td className="px-4 py-1.5 text-white/50">{ytPos?.maturityDate ?? '—'}</td>
                      <td className="px-4 py-1.5 text-right tabular-nums text-rose-300" title={tip}>
                        {fmtAmount(amount)} {ytPos?.ticker ?? ''}
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
        </div>
      )}

      {notFound && (
        <div className="mt-6 border-t border-b border-white/[0.06] px-4 py-3 text-sm text-white/60">
          No timeline data for this wallet. Wallets with fewer than 3 indexed events are not sharded.{' '}
          <a href={`https://solscan.io/account/${addr}`} target="_blank" rel="noopener noreferrer" className="text-white/80 hover:text-white">View on Solscan ↗</a>
        </div>
      )}

      {/* Filter chips */}
      {events && events.length > 0 && (
        <div className="mt-6 flex flex-wrap items-center gap-2">
          <button onClick={() => setEnabled(new Set(allOn ? [] : ALL_FILTERS))}
            className={`text-xs px-3 py-1.5 rounded-full border transition ${allOn ? 'border-white/30 text-white' : 'border-white/[0.08] text-white/40 hover:text-white/70'}`}>
            {allOn ? 'All' : `${enabled.size} selected`}
          </button>
          {ALL_FILTERS.filter(f => actionCounts[f]).map(f => (
            <button key={f} onClick={() => toggle(f)}
              className={`text-xs px-3 py-1.5 rounded-full border transition ${enabled.has(f) ? 'border-white/30 text-white' : 'border-white/[0.08] text-white/40 hover:text-white/70'}`}>
              {LABEL[f]} ({actionCounts[f]})
            </button>
          ))}
        </div>
      )}

      {/* Events table */}
      {events && events.length > 0 && (
        <section className="mt-6 border-t border-b border-white/[0.06] overflow-x-auto">
          <table className="w-full text-sm">
            <thead className="text-[11px] uppercase tracking-[0.18em] text-white/40 border-b border-white/[0.06]">
              <tr>
                <th className="px-4 py-2 text-left cursor-pointer" onClick={() => onSort('date')}>Date{arrow('date')}</th>
                <th className="px-4 py-2 text-left cursor-pointer" onClick={() => onSort('market')}>Market{arrow('market')}</th>
                <th className="px-4 py-2 text-left cursor-pointer" onClick={() => onSort('action')}>Action{arrow('action')}</th>
                <th className="px-4 py-2 text-right cursor-pointer" onClick={() => onSort('usd')}>USD{arrow('usd')}</th>
                <th className="px-4 py-2 text-left">Token Changes</th>
                <th className="px-4 py-2">Tx</th>
              </tr>
            </thead>
            <tbody className="text-[13px]">
              {visible.length === 0 && (
                <tr><td className="px-4 py-3 text-white/30" colSpan={6}>No events match filters.</td></tr>
              )}
              {visible.map((e, i) => (
                <tr key={`${e.sig}-${i}`} className="border-b border-white/[0.06] hover:bg-white/5">
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
    <div className="border-l border-white/[0.08] pl-3 py-1 shrink-0">
      <div className="text-[10px] uppercase tracking-[0.18em] text-white/35 whitespace-nowrap">{label}</div>
      <div className="mt-1 text-[22px] leading-none font-medium tracking-tight tabular-nums text-white whitespace-nowrap">{value}</div>
    </div>
  );
}

// Apple-style inline copy icon. Switches to a checkmark briefly after click.
function CopyIcon({ onClick, copied }: { onClick: () => void; copied: boolean }) {
  return (
    <button onClick={onClick}
            title={copied ? 'Copied' : 'Copy full address'}
            className={`inline-flex items-center justify-center w-6 h-6 rounded transition ${
              copied ? 'text-emerald-300' : 'text-white/30 hover:text-white/70 hover:bg-white/5'
            }`}>
      {copied ? (
        <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5" strokeLinecap="round" strokeLinejoin="round">
          <polyline points="20 6 9 17 4 12" />
        </svg>
      ) : (
        <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
          <rect x="9" y="9" width="13" height="13" rx="2" ry="2" />
          <path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1" />
        </svg>
      )}
    </button>
  );
}

export default function WalletPage() {
  return (
    <Suspense fallback={<div className="mx-auto max-w-[1400px] px-4 py-10 text-white/50">Loading…</div>}>
      <WalletDetailView />
    </Suspense>
  );
}
