'use client';
import { useEffect, useMemo, useState } from 'react';

type ApLeg = { byMarket: Record<string, number[]>; totals: number[] };
type ApTicker = { underlyingMint: string; latest: Record<string, number>; legs: Record<string, ApLeg> };
type ApData = { byTicker: Record<string, ApTicker>; meta: { tickers: { ticker: string; marketCount: number }[] } };

type TvlByMarket = Record<string, {
  ticker: string; platform: string; tvlUsd: number[];
  ptUsd?: number[]; ytUsd?: number[]; lpUsd?: number[]; idleUsd?: number[];
  isTest?: boolean;
  liquidityUsd?: number;   // matches Exponent UI Liquidity (SDK formula on-chain)
}>;
type TvlData = { byMarket: TvlByMarket };

type Holders = {
  byMarketLeg: Record<string, { holders: number }>;
  holdersByMarket?: Record<string, number>;
};

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

const MONTH_ABBR_TO_NUM: Record<string, number> = {
  JAN: 0, FEB: 1, MAR: 2, APR: 3, MAY: 4, JUN: 5,
  JUL: 6, AUG: 7, SEP: 8, OCT: 9, NOV: 10, DEC: 11,
};
// Parse the trailing DDMonYY in a market_key (e.g. 'fragSOL-15DEC26').
// Returns null for keys without a parseable date suffix.
function maturityMs(marketKey: string): number | null {
  const m = marketKey.match(/-(\d{2})([A-Z]{3})(\d{2})$/);
  if (!m) return null;
  const month = MONTH_ABBR_TO_NUM[m[2]];
  if (month === undefined) return null;
  return Date.UTC(2000 + Number(m[3]), month, Number(m[1]));
}

type SortKey =
  | 'marketKey' | 'platform' | 'tvlUsd' | 'ptUsd' | 'ytUsd' | 'lpUsd' | 'idleUsd'
  | 'liquidityUsd' | 'holders';

export function MarketsList() {
  const [tvl, setTvl] = useState<TvlData | null>(null);
  const [ap, setAp] = useState<ApData | null>(null);
  const [holders, setHolders] = useState<Holders | null>(null);
  const [sortKey, setSortKey] = useState<SortKey>('tvlUsd');
  const [sortDesc, setSortDesc] = useState<boolean>(true);
  const [status, setStatus] = useState<StatusFilter>('active');

  useEffect(() => {
    fetch('/tvl.json').then(r => r.json()).then(setTvl).catch(() => null);
    fetch('/active_positions.json').then(r => r.json()).then(setAp).catch(() => null);
    fetch('/market_holders.json').then(r => r.json()).then(setHolders).catch(() => null);
  }, []);

  const rows = useMemo(() => {
    if (!tvl || !ap) return [];
    // Compare to TODAY's UTC midnight (calendar-day), not Date.now(). A
    // market maturing 'today' parses to midnight UTC; using Date.now()
    // would flip it to expired the moment the day starts. Matches the
    // server-side `maturity_date >= current_date` logic in stg_markets.
    const now = new Date();
    const todayMs = Date.UTC(now.getUTCFullYear(), now.getUTCMonth(), now.getUTCDate());
    const out: { marketKey: string; ticker: string; platform: string; tvlUsd: number; ptUsd: number; ytUsd: number; lpUsd: number; idleUsd: number; liquidityUsd: number; ptSupply: number; holders: number; isActive: boolean; isTest: boolean }[] = [];
    for (const [mk, m] of Object.entries(tvl.byMarket)) {
      const ticker = m.ticker;
      const platform = m.platform || 'Other';
      const last = (arr?: number[]) => (arr && arr.length ? arr[arr.length - 1] || 0 : 0);
      const tvlUsd = last(m.tvlUsd);
      // TVL decomposition (PT + YT + LP + Idle sums to tvlUsd by construction).
      const ptUsd   = last(m.ptUsd);
      const ytUsd   = last(m.ytUsd);
      const lpUsd   = last(m.lpUsd);
      const idleUsd = last(m.idleUsd);
      // Liquidity = Exponent SDK formula computed on-chain (matches their UI).
      const liquidityUsd = m.liquidityUsd ?? lpUsd;
      const t = ap.byTicker[ticker];
      const ptSupply = t?.legs.PT?.byMarket?.[mk]?.slice(-1)[0] || 0;
      // Unique holders across PT + YT + LP for this market.
      const h = holders?.holdersByMarket?.[mk] ?? holders?.byMarketLeg?.[`${mk}:PT`]?.holders ?? 0;
      // Active = trailing DDMonYY in the market_key is on or after today.
      const mat = maturityMs(mk);
      const isActive = mat !== null ? mat >= todayMs : tvlUsd > 1 || ptSupply > 0;
      out.push({ marketKey: mk, ticker, platform, tvlUsd, ptUsd, ytUsd, lpUsd, idleUsd, liquidityUsd, ptSupply, holders: h, isActive, isTest: !!m.isTest });
    }
    const filtered = out.filter(r => status === 'all' || (r.isActive && !r.isTest));
    filtered.sort((a, b) => {
      let cmp: number;
      if (sortKey === 'marketKey') {
        // Sort by parsed maturity date — alphabetical is meaningless across
        // markets. Keys without a date suffix (UNCLASSIFIED, etc.) fall to
        // the bottom regardless of direction.
        const am = maturityMs(a.marketKey);
        const bm = maturityMs(b.marketKey);
        if (am === null && bm === null) cmp = a.marketKey.localeCompare(b.marketKey);
        else if (am === null) cmp = 1;
        else if (bm === null) cmp = -1;
        else cmp = am - bm;
      } else if (sortKey === 'platform') {
        cmp = a.platform.localeCompare(b.platform);
      } else {
        const av = a[sortKey] as number;
        const bv = b[sortKey] as number;
        cmp = (Number(av) || 0) - (Number(bv) || 0);
      }
      return sortDesc ? -cmp : cmp;
    });
    return filtered;
  }, [tvl, ap, holders, status, sortKey, sortDesc]);

  function toggleSort(k: SortKey) {
    if (k === sortKey) setSortDesc(d => !d);
    else { setSortKey(k); setSortDesc(true); }   // numeric desc / latest maturity first
  }
  const arrow = (k: SortKey) => k === sortKey ? (sortDesc ? '↓' : '↑') : '';

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
              {([
                ['marketKey',    'Market',    'left',  ''],
                ['platform',     'Platform',  'left',  ''],
                ['tvlUsd',       'TVL',       'right', ''],
                ['ptUsd',        'PT',        'right', 'Principal — market-priced PT value (PT_supply × pt_price × underlying USD)'],
                ['ytUsd',        'YT',        'right', 'Farm — market-priced YT value (YT_supply × yt_price × underlying USD)'],
                ['lpUsd',        'LP',        'right', 'LP value — user share of pool capital (LP supply × underlying USD, capped at remaining SY after PT+YT)'],
                ['idleUsd',      'Idle',      'right', 'Idle SY — TVL not yet split into PT/YT and not in LP'],
                ['liquidityUsd', 'Liquidity', 'right', "AMM pool TVL — matches Exponent UI 'Liquidity'. Formula: sy_balance × sy_rate + pt_balance / exp(last_ln_implied × years_remaining), decoded from the on-chain MarketTwo account."],
                ['holders',      'Holders',   'right', 'Unique wallets holding PT, YT, or LP — a wallet across multiple legs counts once'],
              ] as const).map(([key, label, align, tip]) => (
                <th key={key} title={tip || undefined}
                    className={`py-2 font-normal cursor-pointer select-none hover:text-white/70 ${align === 'left' ? 'text-left' : 'text-right'}`}
                    onClick={() => toggleSort(key as SortKey)}>
                  {label}<span className="ml-1 text-white/30">{arrow(key as SortKey)}</span>
                </th>
              ))}
            </tr>
          </thead>
          <tbody>
            {rows.slice(0, 100).map(r => (
              <tr key={r.marketKey} className="border-b border-white/5 hover:bg-white/5 cursor-pointer"
                  onClick={() => window.location.href = `/market/?key=${r.marketKey}`}>
                <td className="py-1.5 text-white/85">
                  {r.marketKey}
                  {r.isTest && (
                    <span className="ml-2 text-[10px] uppercase tracking-wider px-1.5 py-0.5 rounded border border-amber-400/30 bg-amber-400/10 text-amber-300" title="Test/calibration deployment — never went into production">
                      test
                    </span>
                  )}
                </td>
                <td className="py-1.5 text-white/60">{r.platform}</td>
                <td className="py-1.5 text-right tabular-nums text-white/80">{fmtUsd(r.tvlUsd)}</td>
                <td className="py-1.5 text-right tabular-nums text-white/70">{r.ptUsd > 0 ? fmtUsd(r.ptUsd) : '–'}</td>
                <td className="py-1.5 text-right tabular-nums text-white/70">{r.ytUsd > 0.5 ? fmtUsd(r.ytUsd) : '–'}</td>
                <td className="py-1.5 text-right tabular-nums text-white/70">{r.lpUsd > 0 ? fmtUsd(r.lpUsd) : '–'}</td>
                <td className="py-1.5 text-right tabular-nums text-white/70">{r.idleUsd > 0 ? fmtUsd(r.idleUsd) : '–'}</td>
                <td className="py-1.5 text-right tabular-nums text-white/70">{r.liquidityUsd > 0 ? fmtUsd(r.liquidityUsd) : '–'}</td>
                <td className="py-1.5 text-right tabular-nums text-white/70">{r.holders || '–'}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      {rows.length > 100 && <div className="text-xs text-white/30 mt-2 text-center">Showing top 100 of {rows.length} markets</div>}
    </section>
  );
}
