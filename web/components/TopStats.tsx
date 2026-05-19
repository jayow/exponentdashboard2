'use client';
import { useEffect, useState } from 'react';

type TvlMeta = {
  meta: { dateRange: [string, string]; currentTvlUsd: number; currentPrincipalUsd?: number };
};
type VolMeta = {
  meta: { lifetimeUsd?: number; thirtyDayUsd?: number; dateRange?: [string, string] };
};
type ApMeta = { meta: { tickers: { ticker: string; marketCount: number }[] } };

function fmtUsd(n: number) {
  if (Math.abs(n) >= 1e9) return `$${(n / 1e9).toFixed(2)}B`;
  if (Math.abs(n) >= 1e6) return `$${(n / 1e6).toFixed(2)}M`;
  if (Math.abs(n) >= 1e3) return `$${(n / 1e3).toFixed(1)}K`;
  return `$${n.toFixed(2)}`;
}

export function TopStats() {
  const [tvl, setTvl] = useState<TvlMeta | null>(null);
  const [vol, setVol] = useState<VolMeta | null>(null);
  const [ap, setAp] = useState<ApMeta | null>(null);

  useEffect(() => {
    fetch('/tvl.json').then(r => r.json()).then(setTvl).catch(() => null);
    fetch('/volume.json').then(r => r.json()).then(setVol).catch(() => null);
    fetch('/active_positions.json').then(r => r.json()).then(setAp).catch(() => null);
  }, []);

  const cards: { label: string; value: string; sub?: string }[] = [
    {
      label: 'TVL',
      value: tvl ? fmtUsd(tvl.meta.currentTvlUsd) : '—',
      sub: tvl?.meta.currentPrincipalUsd != null
        ? `principal ${fmtUsd(tvl.meta.currentPrincipalUsd)}`
        : 'SY × underlying',
    },
    {
      label: 'Volume (lifetime)',
      value: vol?.meta.lifetimeUsd != null ? fmtUsd(vol.meta.lifetimeUsd) : '—',
      sub: vol?.meta.thirtyDayUsd != null ? `30d ${fmtUsd(vol.meta.thirtyDayUsd)}` : undefined,
    },
    {
      label: 'Tickers',
      value: ap ? String(ap.meta.tickers.length) : '—',
      sub: ap ? `${ap.meta.tickers.reduce((s, t) => s + t.marketCount, 0)} active markets` : undefined,
    },
  ];

  return (
    <section className="grid grid-cols-1 sm:grid-cols-3 gap-3 mb-6">
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
