'use client';
import { useEffect, useState } from 'react';

type Stats = {
  meta: { generatedAt: string };
  markets: { active: number; expired: number; total: number; platforms: number; tickers: number; latestMaturity: string | null };
  tvl: { currentUsd: number; currentPrincipalUsd: number; peakUsd: number; peakDate: string | null };
  volume: { lifetimeUsd: number; thirty30Usd: number };
  protocol: { firstActivityDate: string | null; ageDays: number | null };
};

function fmtUsd(n: number) {
  if (Math.abs(n) >= 1e9) return `$${(n / 1e9).toFixed(2)}B`;
  if (Math.abs(n) >= 1e6) return `$${(n / 1e6).toFixed(2)}M`;
  if (Math.abs(n) >= 1e3) return `$${(n / 1e3).toFixed(1)}K`;
  return `$${n.toFixed(2)}`;
}
function fmtDate(s: string | null) {
  if (!s) return '—';
  const d = new Date(s);
  return d.toLocaleDateString('en-US', { month: 'short', day: 'numeric', year: 'numeric' });
}

export function TopStats() {
  const [s, setS] = useState<Stats | null>(null);
  useEffect(() => { fetch('/stats.json').then(r => r.json()).then(setS).catch(() => null); }, []);

  const cards: { label: string; value: string; sub?: string }[] = s ? [
    {
      label: 'TVL',
      value: fmtUsd(s.tvl.currentUsd),
      sub: `principal ${fmtUsd(s.tvl.currentPrincipalUsd)} • peak ${fmtUsd(s.tvl.peakUsd)} on ${fmtDate(s.tvl.peakDate)}`,
    },
    {
      label: 'Volume',
      value: fmtUsd(s.volume.lifetimeUsd),
      sub: `30d ${fmtUsd(s.volume.thirty30Usd)} • lifetime`,
    },
    {
      label: 'Markets',
      value: `${s.markets.active} active`,
      sub: `${s.markets.expired} expired • ${s.markets.total} total`,
    },
    {
      label: 'Coverage',
      value: `${s.markets.tickers} tickers`,
      sub: `${s.markets.platforms} platforms • ${s.protocol.ageDays ?? '—'} days indexed`,
    },
  ] : [];

  if (!s) return <section className="mb-6"><div className="text-white/40 text-sm">Loading stats…</div></section>;

  return (
    <section className="grid grid-cols-2 lg:grid-cols-4 gap-3 mb-6">
      {cards.map(c => (
        <div key={c.label} className="rounded-2xl border border-white/10 bg-white/5 p-4">
          <div className="text-[11px] uppercase tracking-wider text-white/40">{c.label}</div>
          <div className="text-2xl font-semibold tabular-nums text-white mt-1">{c.value}</div>
          {c.sub && <div className="text-xs text-white/40 mt-1">{c.sub}</div>}
        </div>
      ))}
    </section>
  );
}
