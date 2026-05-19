'use client';
import { useEffect, useMemo, useState } from 'react';

type ApLeg = { byMarket: Record<string, number[]>; totals: number[] };
type ApTicker = { underlyingMint: string; latest: Record<string, number>; legs: Record<string, ApLeg> };
type ApData = { byTicker: Record<string, ApTicker>; meta: { tickers: { ticker: string; marketCount: number }[] } };

type TvlByMarket = Record<string, { ticker: string; tvlUsd: number[]; principalUsd?: number[] }>;
type TvlData = { byMarket: TvlByMarket };

type Holders = { byMarketLeg: Record<string, { holders: number }> };

type StatusFilter = 'active' | 'all';

function fmtUsd(n: number) {
  if (!n) return '–';
  if (n >= 1e9) return `$${(n / 1e9).toFixed(2)}B`;
  if (n >= 1e6) return `$${(n / 1e6).toFixed(2)}M`;
  if (n >= 1e3) return `$${(n / 1e3).toFixed(1)}K`;
  return `$${n.toFixed(2)}`;
}
function fmtCount(n: number, ticker: string) {
  if (Math.abs(n) >= 1e9) return `${(n / 1e9).toFixed(2)}B ${ticker}`;
  if (Math.abs(n) >= 1e6) return `${(n / 1e6).toFixed(2)}M ${ticker}`;
  if (Math.abs(n) >= 1e3) return `${(n / 1e3).toFixed(1)}K ${ticker}`;
  return `${n.toFixed(0)} ${ticker}`;
}

export function MarketsList() {
  const [tvl, setTvl] = useState<TvlData | null>(null);
  const [ap, setAp] = useState<ApData | null>(null);
  const [holders, setHolders] = useState<Holders | null>(null);
  const [status, setStatus] = useState<StatusFilter>('active');

  useEffect(() => {
    fetch('/tvl.json').then(r => r.json()).then(setTvl).catch(() => null);
    fetch('/active_positions.json').then(r => r.json()).then(setAp).catch(() => null);
    fetch('/market_holders.json').then(r => r.json()).then(setHolders).catch(() => null);
  }, []);

  const rows = useMemo(() => {
    if (!tvl || !ap) return [];
    const out: { marketKey: string; ticker: string; tvlUsd: number; pt: number; yt: number; lp: number; holders: number; isActive: boolean }[] = [];
    for (const [mk, m] of Object.entries(tvl.byMarket)) {
      const ticker = m.ticker;
      const tvlUsd = m.tvlUsd[m.tvlUsd.length - 1] || 0;
      const t = ap.byTicker[ticker];
      const pt = t?.legs.PT?.byMarket?.[mk]?.slice(-1)[0] || 0;
      const yt = t?.legs.YT?.byMarket?.[mk]?.slice(-1)[0] || 0;
      const lp = t?.legs.LP?.byMarket?.[mk]?.slice(-1)[0] || 0;
      const h = holders?.byMarketLeg?.[`${mk}:PT`]?.holders || 0;
      // Active = market_key date suffix is in the future (rough heuristic)
      const suffix = mk.split('-').slice(-1)[0]; // DDMonYY
      const isActive = tvlUsd > 1 || pt > 0;
      out.push({ marketKey: mk, ticker, tvlUsd, pt, yt, lp, holders: h, isActive });
    }
    return out
      .filter(r => status === 'all' || r.isActive)
      .sort((a, b) => b.tvlUsd - a.tvlUsd);
  }, [tvl, ap, holders, status]);

  if (!tvl || !ap) return <div className="text-white/40 text-sm p-4">Loading markets…</div>;

  return (
    <section className="rounded-2xl border border-white/10 bg-white/5 p-4 mb-6">
      <header className="flex items-baseline justify-between flex-wrap gap-2 mb-3">
        <div>
          <h2 className="text-sm uppercase tracking-wider text-white/60">Markets</h2>
          <p className="text-xs text-white/40">{rows.length} markets • click for detail</p>
        </div>
        <div className="flex items-center gap-1">
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
              <th className="text-right py-2 font-normal">TVL</th>
              <th className="text-right py-2 font-normal">PT supply</th>
              <th className="text-right py-2 font-normal">YT supply</th>
              <th className="text-right py-2 font-normal">LP supply</th>
              <th className="text-right py-2 font-normal">Holders</th>
            </tr>
          </thead>
          <tbody>
            {rows.slice(0, 100).map(r => (
              <tr key={r.marketKey} className="border-b border-white/5 hover:bg-white/5 cursor-pointer"
                  onClick={() => window.location.href = `/market/?key=${r.marketKey}`}>
                <td className="py-1.5 text-white/85">{r.marketKey}</td>
                <td className="py-1.5 text-right tabular-nums text-emerald-400/80">{fmtUsd(r.tvlUsd)}</td>
                <td className="py-1.5 text-right tabular-nums text-white/70">{fmtCount(r.pt, r.ticker)}</td>
                <td className="py-1.5 text-right tabular-nums text-white/70">{fmtCount(r.yt, r.ticker)}</td>
                <td className="py-1.5 text-right tabular-nums text-white/70">{r.lp > 0 ? fmtCount(r.lp, r.ticker) : '–'}</td>
                <td className="py-1.5 text-right tabular-nums text-white/60">{r.holders || '–'}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      {rows.length > 100 && <div className="text-xs text-white/30 mt-2 text-center">Showing top 100 of {rows.length} markets</div>}
    </section>
  );
}
