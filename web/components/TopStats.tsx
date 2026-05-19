'use client';
import { useEffect, useState } from 'react';

type Stats = {
  meta: { generatedAt: string };
  markets: { active: number; expired: number; total: number; platforms: number; tickers: number; latestMaturity: string | null };
  tvl: { currentUsd: number; currentPrincipalUsd: number; peakUsd: number; peakDate: string | null;
         ptUsd: number; lpUsd: number; idleUsd: number };
  volume: { lifetimeUsd: number; thirty30Usd: number };
  holders: { totalUniqueOwners: number };
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

export function TopStats() {
  const [s, setS] = useState<Stats | null>(null);
  useEffect(() => { fetch('/stats.json').then(r => r.json()).then(setS).catch(() => null); }, []);
  if (!s) return <div className="mb-6 text-white/40 text-sm">Loading stats…</div>;

  // Hero (left) + grid of small stats (right). Mirrors v1's layout.
  const small: { label: string; value: string }[] = [
    { label: 'Income (PT)',     value: fmtUsd(s.tvl.ptUsd) },
    { label: 'Liquidity (LP)',  value: fmtUsd(s.tvl.lpUsd) },
    { label: 'Idle',            value: fmtUsd(s.tvl.idleUsd) },
    { label: 'All-Time Volume', value: fmtUsd(s.volume.lifetimeUsd) },
    { label: 'Peak TVL',        value: fmtUsd(s.tvl.peakUsd) },
    { label: 'Holders',         value: s.holders.totalUniqueOwners.toLocaleString() },
    { label: 'Markets',         value: `${s.markets.active} / ${s.markets.total}` },
    { label: 'Platforms',       value: String(s.markets.platforms) },
    { label: 'Age',             value: s.protocol.ageDays != null ? `${s.protocol.ageDays}d` : '—' },
  ];

  return (
    <section className="mb-6 flex flex-wrap items-end gap-x-10 gap-y-4">
      <div>
        <div className="text-[11px] uppercase tracking-wider text-white/40">Protocol TVL</div>
        <div className="text-4xl font-semibold tabular-nums text-white mt-1">{fmtUsd(s.tvl.currentUsd)}</div>
        <div className="text-xs text-white/30 mt-1">peak {fmtUsd(s.tvl.peakUsd)} on {fmtDate(s.tvl.peakDate)}</div>
      </div>
      <div className="grid grid-cols-3 md:grid-cols-5 lg:grid-cols-9 gap-x-6 gap-y-2 flex-1">
        {small.map(c => (
          <div key={c.label}>
            <div className="text-[10px] uppercase tracking-wider text-white/40 whitespace-nowrap">{c.label}</div>
            <div className="text-base font-semibold tabular-nums text-white mt-0.5 whitespace-nowrap">{c.value}</div>
          </div>
        ))}
      </div>
    </section>
  );
}
