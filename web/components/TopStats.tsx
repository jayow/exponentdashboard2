'use client';
import { useEffect, useState } from 'react';

type Stats = {
  meta: { generatedAt: string };
  markets: { active: number; expired: number; total: number; platforms: number; tickers: number; latestMaturity: string | null };
  tvl: {
    currentUsd: number; currentPrincipalUsd: number; peakUsd: number; peakDate: string | null;
    ptUsd: number; ytUsd: number; lpUsd: number; idleUsd: number;
    weekAgo: { totalUsd: number; ptUsd: number; ytUsd: number; lpUsd: number; idleUsd: number };
  };
  volume: { lifetimeUsd: number; thirty30Usd: number; sevenDayUsd: number };
  holders: { totalUniqueOwners: number; weekAgo: number; newSevenDay: number };
  marketsActiveWeekAgo: number;
  protocol: { firstActivityDate: string | null; ageDays: number | null };
};

function fmtUsd(n: number) {
  if (Math.abs(n) >= 1e9) return `$${(n / 1e9).toFixed(2)}B`;
  if (Math.abs(n) >= 1e6) return `$${(n / 1e6).toFixed(2)}M`;
  if (Math.abs(n) >= 1e3) return `$${(n / 1e3).toFixed(1)}K`;
  return `$${n.toFixed(0)}`;
}

// Color delta by direction; always include "7d" suffix so the timeframe
// is explicit. `good` controls whether ▲ is treated as positive/green
// (`up`) or negative/rose (`down`) — e.g. Idle going UP is neutral-to-bad.
function Delta({ now, then, kind = 'usd', good = 'up' }:
  { now: number; then: number; kind?: 'usd' | 'count'; good?: 'up' | 'down' | 'neutral' }) {
  if (then == null || then === 0) return <span className="text-white/20 text-[10px]">— 7d</span>;
  const diff = now - then;
  if (!isFinite(diff) || diff === 0) return <span className="text-white/20 text-[10px]">flat 7d</span>;
  const pct = (diff / then) * 100;
  const up = diff > 0;
  let cls = 'text-white/40';
  if (good === 'up')      cls = up ? 'text-emerald-400' : 'text-rose-400';
  else if (good === 'down') cls = up ? 'text-rose-400' : 'text-emerald-400';
  const arrow = up ? '▲' : '▼';
  const abs = kind === 'usd' ? fmtUsd(Math.abs(diff)) : Math.abs(diff).toLocaleString();
  return (
    <span className={`text-[10px] tabular-nums ${cls}`}>
      {arrow} {abs} ({Math.abs(pct).toFixed(1)}%) vs 7d
    </span>
  );
}

// "+N {what} 7d" addition — green if positive, neutral if zero. `what` is
// an optional noun describing what the count represents (e.g. "new").
function Plus7d({ amount, kind = 'usd', what }:
  { amount: number; kind?: 'usd' | 'count'; what?: string }) {
  if (amount <= 0) return <span className="text-white/20 text-[10px]">— 7d</span>;
  const label = kind === 'usd' ? fmtUsd(amount) : amount.toLocaleString();
  return (
    <span className="text-[10px] tabular-nums text-emerald-400">
      +{label}{what ? ` ${what}` : ''} 7d
    </span>
  );
}

export function TopStats() {
  const [s, setS] = useState<Stats | null>(null);
  useEffect(() => { fetch('/stats.json').then(r => r.json()).then(setS).catch(() => null); }, []);
  if (!s) return <div className="mb-6 text-white/40 text-sm">Loading stats…</div>;

  type SmallCard = { label: string; value: string; delta?: React.ReactNode };
  const small: SmallCard[] = [
    {
      label: 'Income (PT)',
      value: fmtUsd(s.tvl.ptUsd),
      delta: <Delta now={s.tvl.ptUsd} then={s.tvl.weekAgo.ptUsd} kind="usd" good="up" />,
    },
    {
      label: 'Farm (YT)',
      value: fmtUsd(s.tvl.ytUsd),
      delta: <Delta now={s.tvl.ytUsd} then={s.tvl.weekAgo.ytUsd} kind="usd" good="up" />,
    },
    {
      label: 'Liquidity (LP)',
      value: fmtUsd(s.tvl.lpUsd),
      delta: <Delta now={s.tvl.lpUsd} then={s.tvl.weekAgo.lpUsd} kind="usd" good="up" />,
    },
    {
      label: 'Idle',
      value: fmtUsd(s.tvl.idleUsd),
      // Idle going UP = capital sitting unused → mildly bad; down = good
      delta: <Delta now={s.tvl.idleUsd} then={s.tvl.weekAgo.idleUsd} kind="usd" good="down" />,
    },
    {
      label: 'All-Time Volume',
      value: fmtUsd(s.volume.lifetimeUsd),
      delta: <Plus7d amount={s.volume.sevenDayUsd} kind="usd" />,
    },
    {
      label: 'Peak TVL',
      value: fmtUsd(s.tvl.peakUsd),
    },
    {
      label: 'Holders',
      value: s.holders.totalUniqueOwners.toLocaleString(),
      // "+N new 7d" — wallets that became active for the first time
      delta: <Plus7d amount={s.holders.newSevenDay} kind="count" what="new" />,
    },
    {
      label: 'Markets',
      value: `${s.markets.active} / ${s.markets.total}`,
    },
    {
      label: 'Platforms',
      value: String(s.markets.platforms),
    },
    {
      label: 'Age',
      value: s.protocol.ageDays != null ? `${s.protocol.ageDays}d` : '—',
    },
  ];

  return (
    <section className="mb-6">
      {/* Hero */}
      <div className="mb-5">
        <div className="text-[11px] uppercase tracking-wider text-white/40">Protocol TVL</div>
        <div className="text-4xl font-semibold tabular-nums text-white mt-1">{fmtUsd(s.tvl.currentUsd)}</div>
        <div className="mt-1">
          <Delta now={s.tvl.currentUsd} then={s.tvl.weekAgo.totalUsd} kind="usd" good="up" />
        </div>
      </div>

      {/* Stat grid — 2/3/5 cols depending on viewport, generous spacing */}
      <div className="grid grid-cols-2 sm:grid-cols-3 md:grid-cols-5 gap-x-8 gap-y-5">
        {small.map(c => (
          <div key={c.label} className="min-w-0">
            <div className="text-[10px] uppercase tracking-wider text-white/40 truncate">{c.label}</div>
            <div className="text-base font-semibold tabular-nums text-white mt-1 truncate">{c.value}</div>
            {c.delta && <div className="mt-1 truncate">{c.delta}</div>}
          </div>
        ))}
      </div>
    </section>
  );
}
