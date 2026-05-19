'use client';
import { useEffect, useState } from 'react';

type Stats = {
  meta: { generatedAt: string };
  markets: { active: number; expired: number; total: number; platforms: number; tickers: number; latestMaturity: string | null };
  tvl: {
    currentUsd: number; currentPrincipalUsd: number; peakUsd: number; peakDate: string | null;
    ptUsd: number; lpUsd: number; idleUsd: number;
    weekAgo: { totalUsd: number; ptUsd: number; lpUsd: number; idleUsd: number };
  };
  volume: { lifetimeUsd: number; thirty30Usd: number; sevenDayUsd: number };
  holders: { totalUniqueOwners: number; weekAgo: number };
  marketsActiveWeekAgo: number;
  protocol: { firstActivityDate: string | null; ageDays: number | null };
};

function fmtUsd(n: number) {
  if (Math.abs(n) >= 1e9) return `$${(n / 1e9).toFixed(2)}B`;
  if (Math.abs(n) >= 1e6) return `$${(n / 1e6).toFixed(2)}M`;
  if (Math.abs(n) >= 1e3) return `$${(n / 1e3).toFixed(1)}K`;
  return `$${n.toFixed(0)}`;
}
function fmtDate(s: string | null) {
  if (!s) return '—';
  const d = new Date(s);
  return d.toLocaleDateString('en-US', { month: 'short', day: 'numeric', year: '2-digit' });
}

function Delta({ now, then, kind }: { now: number; then: number; kind: 'usd' | 'count' }) {
  if (!then) return <span className="text-white/20 text-[10px]">—</span>;
  const diff = now - then;
  if (!isFinite(diff) || diff === 0) return <span className="text-white/20 text-[10px]">flat</span>;
  const pct = (diff / then) * 100;
  const up = diff > 0;
  const cls = up ? 'text-emerald-400' : 'text-rose-400';
  const arrow = up ? '▲' : '▼';
  const abs = kind === 'usd' ? fmtUsd(Math.abs(diff)) : Math.abs(diff).toLocaleString();
  return (
    <span className={`text-[10px] tabular-nums ${cls}`}>
      {arrow} {abs} ({Math.abs(pct).toFixed(1)}%)
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
      delta: <Delta now={s.tvl.ptUsd} then={s.tvl.weekAgo.ptUsd} kind="usd" />,
    },
    {
      label: 'Liquidity (LP)',
      value: fmtUsd(s.tvl.lpUsd),
      delta: <Delta now={s.tvl.lpUsd} then={s.tvl.weekAgo.lpUsd} kind="usd" />,
    },
    {
      label: 'Idle',
      value: fmtUsd(s.tvl.idleUsd),
      delta: <Delta now={s.tvl.idleUsd} then={s.tvl.weekAgo.idleUsd} kind="usd" />,
    },
    {
      label: 'All-Time Volume',
      value: fmtUsd(s.volume.lifetimeUsd),
      delta: <span className="text-[10px] text-white/40">+{fmtUsd(s.volume.sevenDayUsd)} 7d</span>,
    },
    {
      label: 'Peak TVL',
      value: fmtUsd(s.tvl.peakUsd),
    },
    {
      label: 'Holders',
      value: s.holders.totalUniqueOwners.toLocaleString(),
      delta: s.holders.weekAgo > 0
        ? <Delta now={s.holders.totalUniqueOwners} then={s.holders.weekAgo} kind="count" />
        : <span className="text-white/20 text-[10px]">first snapshot</span>,
    },
    {
      label: 'Markets',
      value: `${s.markets.active} / ${s.markets.total}`,
      delta: <Delta now={s.markets.active} then={s.marketsActiveWeekAgo} kind="count" />,
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
    <section className="mb-6 flex flex-wrap items-end gap-x-10 gap-y-4">
      <div>
        <div className="text-[11px] uppercase tracking-wider text-white/40">Protocol TVL</div>
        <div className="text-4xl font-semibold tabular-nums text-white mt-1">{fmtUsd(s.tvl.currentUsd)}</div>
        <div className="mt-1 flex items-center gap-2">
          <Delta now={s.tvl.currentUsd} then={s.tvl.weekAgo.totalUsd} kind="usd" />
          <span className="text-xs text-white/30">vs 7d ago • peak {fmtUsd(s.tvl.peakUsd)} on {fmtDate(s.tvl.peakDate)}</span>
        </div>
      </div>
      <div className="grid grid-cols-3 md:grid-cols-5 lg:grid-cols-9 gap-x-6 gap-y-3 flex-1">
        {small.map(c => (
          <div key={c.label}>
            <div className="text-[10px] uppercase tracking-wider text-white/40 whitespace-nowrap">{c.label}</div>
            <div className="text-base font-semibold tabular-nums text-white mt-0.5 whitespace-nowrap">{c.value}</div>
            {c.delta && <div className="mt-0.5 whitespace-nowrap">{c.delta}</div>}
          </div>
        ))}
      </div>
    </section>
  );
}
