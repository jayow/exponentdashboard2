'use client';
import { useEffect, useMemo, useState } from 'react';
import { projectTrancheApy } from '@/lib/tranche-math';

type TrancheSide = {
  apy: number | null;
  navUsd: number | null;
  effectiveNavUsd: number | null;
  lpPrice: number | null;
  maxCapacityUsd: number | null;
  remainingUsd: number | null;
  pointsMult: number | null;
  lpMint: string | null;
};

type TrancheHistory = {
  dates: string[];
  coverageRatio: (number | null)[];
  srNavUsd: number[];
  jrNavUsd: number[];
  utilizationPct: number[];
  currentJrReturnShare: (number | null)[];
};

type LpActionStats = {
  actions: number;
  uniqueWallets: number;
  deposits: number;
  withdrawals: number;
};

type TopLp = { wallet: string; lpUnits: number };
type ActivityEvent = {
  sig: string;
  blockTime: number;
  action: string;
  wallet: string;
  leg: 'SR' | 'JR';
  lpUnits: number;
};
type CurvePoint = { x: number; y: number };

type Tranche = {
  address: string;
  ticker: string;
  platform: string | null;
  seedId?: string;
  startDate: number | null;
  marketState: number | null;
  underlyingApy: number | null;
  underlyingApy7d: number | null;
  underlyingApy30d: number | null;
  syExchangeRate: number | null;
  coverageRatio: number | null;
  minCoverageRatio: number | null;
  marketSizeUsd: number | null;
  effectiveSizeUsd: number | null;
  utilizationPct: number | null;
  senior: TrancheSide;
  junior: TrancheSide;
  syMint: string | null;
  baseMint: string | null;
  currentJrReturnShare?: number | null;
  twJrReturnShareAccrued?: number | null;
  lastSyncTs?: number | null;
  lastDistributionTs?: number | null;
  fixedTermEndTs?: number | null;
  fixedTermDurationSec?: number | null;
  history?: TrancheHistory;
  lpActions?: Record<string, LpActionStats>;
  topLps?: Record<string, TopLp[]>;
  recentActivity?: ActivityEvent[];
  returnCurve?: CurvePoint[];
};

function shortAddr(a: string) {
  return `${a.slice(0, 4)}…${a.slice(-4)}`;
}

function fmtUnits(n: number) {
  const abs = Math.abs(n);
  if (abs >= 1e6) return `${(n / 1e6).toFixed(2)}M`;
  if (abs >= 1e3) return `${(n / 1e3).toFixed(1)}K`;
  return n.toFixed(2);
}

function fmtTimeAgo(ts: number): string {
  const sec = Math.max(0, Math.floor(Date.now() / 1000) - ts);
  if (sec < 60) return `${sec}s ago`;
  if (sec < 3600) return `${Math.floor(sec / 60)}m ago`;
  if (sec < 86400) return `${Math.floor(sec / 3600)}h ago`;
  return `${Math.floor(sec / 86400)}d ago`;
}

type CurveMarker = {
  x: number;
  label: string;
  color: string;
  dashed?: boolean;
};

function interpY(points: CurvePoint[], x: number): number {
  if (!points.length) return 0;
  if (x <= points[0].x) return points[0].y;
  if (x >= points[points.length - 1].x) return points[points.length - 1].y;
  for (let i = 0; i < points.length - 1; i++) {
    if (x >= points[i].x && x < points[i + 1].x) {
      const t = (x - points[i].x) / (points[i + 1].x - points[i].x);
      return points[i].y + t * (points[i + 1].y - points[i].y);
    }
  }
  return points[points.length - 1].y;
}

function ReturnCurveSpark({
  points, markers,
}: { points: CurvePoint[]; markers: CurveMarker[] }) {
  if (!points.length) return null;
  const W = 320, H = 100;
  const xs = points.map(p => p.x);
  const ys = points.map(p => p.y);
  const xMin = Math.min(...xs), xMax = Math.max(...xs);
  const yMin = Math.min(...ys), yMax = Math.max(...ys);
  const dx = (xMax - xMin) || 1;
  const dy = (yMax - yMin) || 1;
  const xToPx = (x: number) => ((x - xMin) / dx) * (W - 8) + 4;
  const yToPy = (y: number) => H - 4 - ((y - yMin) / dy) * (H - 8);
  const path = points.map((p, i) => `${i === 0 ? 'M' : 'L'}${xToPx(p.x).toFixed(1)},${yToPy(p.y).toFixed(1)}`).join(' ');

  return (
    <svg width={W} height={H + 18} className="overflow-visible">
      <path d={path} stroke="rgba(255,255,255,0.55)" strokeWidth={1.4} fill="none" />
      {markers.map((m, i) => {
        const xClamped = Math.min(Math.max(m.x, xMin), xMax);
        const px = xToPx(xClamped);
        const py = yToPy(interpY(points, xClamped));
        const labelY = i === 0 ? H - 6 : 14;
        return (
          <g key={i}>
            <line x1={px} x2={px} y1={4} y2={H - 4} stroke={m.color} strokeDasharray={m.dashed ? '2 2' : ''} />
            <circle cx={px} cy={py} r={2.8} fill={m.color} />
            <text x={px + 5} y={labelY} fill={m.color} fontSize={9}>{m.label}</text>
          </g>
        );
      })}
      <text x={4} y={H + 14} fill="rgba(255,255,255,0.35)" fontSize={9}>{(xMin * 100).toFixed(0)}%</text>
      <text x={W - 4} y={H + 14} fill="rgba(255,255,255,0.35)" fontSize={9} textAnchor="end">{(xMax * 100).toFixed(0)}%</text>
      <text x={4} y={11} fill="rgba(255,255,255,0.35)" fontSize={9}>{(yMax * 100).toFixed(0)}%</text>
      <text x={4} y={H - 6} fill="rgba(255,255,255,0.35)" fontSize={9}>{(yMin * 100).toFixed(0)}%</text>
    </svg>
  );
}

type TrancheData = {
  meta: { generatedAt: string; source: string; count: number };
  tranches: Tranche[];
};

function fmtUsd(n: number | null) {
  if (n == null) return '–';
  if (n >= 1e6) return `$${(n / 1e6).toFixed(2)}M`;
  if (n >= 1e3) return `$${(n / 1e3).toFixed(1)}K`;
  return `$${n.toFixed(0)}`;
}

function fmtPct(n: number | null, digits = 2) {
  if (n == null) return '–';
  return `${(n * 100).toFixed(digits)}%`;
}

function CapacityBar({ used, total }: { used: number; total: number }) {
  const pct = total > 0 ? Math.min(100, (used / total) * 100) : 0;
  return (
    <div className="h-1 w-full bg-white/[0.06] rounded-full overflow-hidden">
      <div className="h-full bg-white/40" style={{ width: `${pct}%` }} />
    </div>
  );
}

function CurveAndWhatIf({
  curve, srSize, jrSize, underlyingApy, minCoverage, currentUtilization, currentJrShare,
}: {
  curve: CurvePoint[];
  srSize: number;
  jrSize: number;
  underlyingApy: number;
  minCoverage: number;
  currentUtilization: number;
  currentJrShare: number;
}) {
  const totalNAV = srSize + jrSize;
  const currentSrFraction = totalNAV > 0 ? srSize / totalNAV : 0;
  const [srFraction, setSrFraction] = useState(currentSrFraction);

  const scenarioSr = totalNAV * srFraction;
  const scenarioJr = totalNAV * (1 - srFraction);

  const projection = useMemo(() => projectTrancheApy({
    srSize: scenarioSr, jrSize: scenarioJr, underlyingApy, minCoverage, curve,
  }), [scenarioSr, scenarioJr, underlyingApy, minCoverage, curve]);

  const currentProjection = useMemo(() => projectTrancheApy({
    srSize, jrSize, underlyingApy, minCoverage, curve,
  }), [srSize, jrSize, underlyingApy, minCoverage, curve]);

  const scenarioCoverage = 1 - srFraction;
  const scenarioUtilization = scenarioCoverage > 0 ? minCoverage / scenarioCoverage : Number.POSITIVE_INFINITY;

  const fmtPctLocal = (n: number, d = 2) => `${(n * 100).toFixed(d)}%`;
  const fmtUsdLocal = (n: number) => n >= 1e6 ? `$${(n / 1e6).toFixed(2)}M` : n >= 1e3 ? `$${(n / 1e3).toFixed(1)}K` : `$${n.toFixed(0)}`;

  return (
    <div className="mt-6 pt-4 border-t border-white/[0.06]">
      <div className="flex items-baseline justify-between mb-2">
        <div className="text-[10px] uppercase tracking-[0.18em] text-white/35">Return curve & what-if</div>
        <div className="text-[10px] text-white/35">slider drives the marker · 85% confidence</div>
      </div>
      <div className="text-[11px] text-white/50 mb-4 leading-relaxed">
        <b className="text-white/75">X = utilization</b> = min_coverage / coverage, the protocol&apos;s distance-to-floor metric.
        {' '}<b className="text-white/75">Y = junior&apos;s share of yield</b>; senior gets <code className="text-white/55">(1 − y)</code>.
        Drag the slider to crowd in more senior; the curve marker slides right and APYs reprice live.
      </div>

      {/* Trim to reachable range: util has a hard floor at min_coverage, so points
          below that are unreachable by any real pool state. */}
      <div className="flex items-end gap-6 flex-wrap mb-5">
        <ReturnCurveSpark
          points={curve.filter(p => p.x >= minCoverage)}
          markers={[
            { x: currentUtilization, label: 'now', color: 'rgba(255,255,255,0.55)', dashed: true },
            { x: scenarioUtilization, label: 'slider', color: 'rgba(125,211,252,0.95)' },
          ]}
        />
        <div className="text-[11px] space-y-0.5 pb-3 min-w-[180px]">
          <div className="text-white/35 uppercase tracking-wider text-[9px] mb-1">now</div>
          <div className="text-white/70 tabular-nums">x = {fmtPctLocal(currentUtilization, 2)}</div>
          <div className="text-white/70 tabular-nums">y = {fmtPctLocal(currentJrShare, 2)}</div>
          <div className="text-sky-300/60 uppercase tracking-wider text-[9px] mb-1 mt-2">slider</div>
          <div className="text-sky-300/90 tabular-nums">x = {isFinite(scenarioUtilization) ? fmtPctLocal(scenarioUtilization, 2) : '∞'}</div>
          <div className="text-sky-300/90 tabular-nums">y = {fmtPctLocal(projection.juniorReturnShare, 2)}</div>
        </div>
      </div>

      <div className="mb-4">
        <div className="text-[11px] text-white/55 mb-2 flex justify-between">
          <span>Senior fraction of pool</span>
          <span className="text-sky-300/90 tabular-nums">{fmtPctLocal(srFraction, 2)}</span>
        </div>
        <input
          type="range"
          min={0.001}
          max={0.80}
          step={0.001}
          value={srFraction}
          onChange={e => setSrFraction(parseFloat(e.target.value))}
          className="w-full accent-sky-300"
        />
        <div className="flex justify-between text-[9px] text-white/30 mt-1">
          <span>0%</span><span>protocol cap 80% (util = 100%)</span>
        </div>
      </div>

      <div className="text-[10px] uppercase tracking-[0.18em] text-white/35 mb-2">Derivation</div>
      <div className="grid grid-cols-2 md:grid-cols-4 gap-3 mb-5 text-[11px]">
        <div className="border-l border-sky-300/20 pl-3">
          <div className="text-white/45 mb-1">Senior fraction (s)</div>
          <div className="text-white tabular-nums">{fmtPctLocal(srFraction, 2)}</div>
          <div className="text-white/30 text-[10px] mt-0.5">slider input</div>
        </div>
        <div className="border-l border-sky-300/20 pl-3">
          <div className="text-white/45 mb-1">Coverage = 1 − s</div>
          <div className="text-white tabular-nums">{fmtPctLocal(scenarioCoverage, 2)}</div>
          <div className="text-white/30 text-[10px] mt-0.5">jr share of pool</div>
        </div>
        <div className="border-l border-sky-300/20 pl-3">
          <div className="text-white/45 mb-1">Util = m / cov</div>
          <div className="text-white tabular-nums">{isFinite(scenarioUtilization) ? fmtPctLocal(scenarioUtilization, 2) : '∞'}</div>
          <div className="text-white/30 text-[10px] mt-0.5">m = {fmtPctLocal(minCoverage, 0)}</div>
        </div>
        <div className="border-l border-sky-300/20 pl-3">
          <div className="text-white/45 mb-1">Jr share (curve)</div>
          <div className="text-white tabular-nums">{fmtPctLocal(projection.juniorReturnShare, 2)}</div>
          <div className="text-white/30 text-[10px] mt-0.5">read off plot</div>
        </div>
      </div>

      <div className="grid grid-cols-1 md:grid-cols-3 gap-6 mb-3">
        <div className="border-t border-emerald-300/15 pt-2">
          <div className="text-[10px] text-emerald-300/70 uppercase tracking-wider">Senior APY</div>
          <div className="text-2xl text-white tabular-nums mt-0.5">{fmtPctLocal(projection.seniorApy, 2)}</div>
          <div className="text-[10px] text-white/40">
            vs now {fmtPctLocal(currentProjection.seniorApy, 2)} · Δ {fmtPctLocal(projection.seniorApy - currentProjection.seniorApy, 2)}
          </div>
          <div className="text-[10px] text-white/30 mt-0.5">= underlying × (1 − y)</div>
        </div>
        <div className="border-t border-rose-300/15 pt-2">
          <div className="text-[10px] text-rose-300/70 uppercase tracking-wider">Junior APY</div>
          <div className="text-2xl text-white tabular-nums mt-0.5">
            {isFinite(projection.juniorApy) ? fmtPctLocal(projection.juniorApy, 2) : '∞'}
          </div>
          <div className="text-[10px] text-white/40">
            vs now {fmtPctLocal(currentProjection.juniorApy, 2)} · Δ {isFinite(projection.juniorApy) ? fmtPctLocal(projection.juniorApy - currentProjection.juniorApy, 2) : '∞'}
          </div>
          <div className="text-[10px] text-white/30 mt-0.5">= underlying × (1 + y · sr/jr)</div>
        </div>
        <div className="border-t border-white/[0.08] pt-2">
          <div className="text-[10px] text-white/45 uppercase tracking-wider">Scenario sizes</div>
          <div className="text-[11px] text-white/70 mt-1 space-y-0.5">
            <div className="flex justify-between"><span>Sr NAV</span><span className="tabular-nums">{fmtUsdLocal(scenarioSr)}</span></div>
            <div className="flex justify-between"><span>Jr NAV</span><span className="tabular-nums">{fmtUsdLocal(scenarioJr)}</span></div>
            <div className="flex justify-between text-white/40"><span>underlying APY</span><span className="tabular-nums">{fmtPctLocal(underlyingApy, 2)}</span></div>
          </div>
        </div>
      </div>

      <div className="text-[10px] text-white/30 mt-4 leading-relaxed">
        Holds total pool NAV constant. Doesn&apos;t model fees, IL recovery, or liquidation regime (util ≥ 1.1).
        Steady-state projection only.
      </div>
    </div>
  );
}

export function TranchingAnalytics() {
  const [data, setData] = useState<TrancheData | null>(null);
  const [err, setErr] = useState<string | null>(null);

  useEffect(() => {
    fetch('/tranche.json')
      .then(r => (r.ok ? r.json() : Promise.reject(`HTTP ${r.status}`)))
      .then(setData)
      .catch(e => setErr(String(e)));
  }, []);

  if (err) return <div className="text-sm text-rose-400/80">Failed to load tranche data: {err}</div>;
  if (!data) return <div className="text-sm text-white/40">Loading…</div>;

  if (!data.tranches.length) {
    return (
      <div className="border-t border-b border-white/[0.06] py-12 text-center">
        <p className="text-sm text-white/40">No tranching markets live yet.</p>
        <p className="text-[11px] text-white/30 mt-1">When Exponent adds tranches, they'll appear here.</p>
      </div>
    );
  }

  return (
    <div className="space-y-10">
      {data.tranches.map(t => {
        const srUsed = (t.senior.maxCapacityUsd ?? 0) - (t.senior.remainingUsd ?? 0);
        const jrUsed = (t.junior.maxCapacityUsd ?? 0) - (t.junior.remainingUsd ?? 0);
        const startStr = t.startDate ? new Date(t.startDate * 1000).toISOString().slice(0, 10) : '—';
        return (
          <section key={t.address}>
            {/* Title */}
            <div className="flex items-baseline gap-4 mb-5">
              <h2 className="text-xl text-white">{t.ticker}</h2>
              <span className="text-[11px] uppercase tracking-[0.18em] text-white/35">{t.platform ?? 'Tranche'} · since {startStr}</span>
            </div>

            {/* Hero row */}
            <div className="grid grid-cols-2 md:grid-cols-4 gap-x-10 gap-y-4 border-t border-b border-white/[0.06] py-5 mb-6">
              <div>
                <div className="text-[10px] uppercase tracking-[0.18em] text-white/35">Coverage ratio</div>
                <div className="text-2xl text-white tabular-nums mt-1">{fmtPct(t.coverageRatio, 2)}</div>
                <div className="text-[11px] text-white/40 mt-0.5">floor {fmtPct(t.minCoverageRatio, 0)}</div>
              </div>
              <div>
                <div className="text-[10px] uppercase tracking-[0.18em] text-white/35">Total NAV</div>
                <div className="text-2xl text-white tabular-nums mt-1">{fmtUsd(t.marketSizeUsd)}</div>
                <div className="text-[11px] text-white/40 mt-0.5">util {t.utilizationPct?.toFixed(1) ?? '?'}%</div>
              </div>
              <div>
                <div className="text-[10px] uppercase tracking-[0.18em] text-white/35">Underlying APY</div>
                <div className="text-2xl text-white tabular-nums mt-1">{fmtPct(t.underlyingApy, 2)}</div>
                <div className="text-[11px] text-white/40 mt-0.5">SY rate {t.syExchangeRate?.toFixed(4) ?? '?'}</div>
              </div>
              <div>
                <div className="text-[10px] uppercase tracking-[0.18em] text-white/35">Status</div>
                <div className="text-2xl text-emerald-400/85 tabular-nums mt-1">{t.marketState === 1 ? 'Active' : `State ${t.marketState ?? '?'}`}</div>
              </div>
            </div>

            {/* Side-by-side: junior + senior */}
            <div className="grid grid-cols-1 md:grid-cols-2 gap-8">
              {/* Junior */}
              <div className="border-t border-white/[0.06] pt-4">
                <div className="flex items-baseline justify-between mb-3">
                  <div>
                    <div className="text-[10px] uppercase tracking-[0.18em] text-rose-300/70">Junior · first-loss</div>
                    <div className="text-base text-white mt-1">jr{t.ticker}</div>
                  </div>
                  <div className="text-right">
                    <div className="text-2xl text-white tabular-nums">{fmtPct(t.junior.apy, 2)}</div>
                    <div className="text-[11px] text-white/40">{t.junior.pointsMult ? `${t.junior.pointsMult}× points` : ''}</div>
                  </div>
                </div>
                <div className="space-y-2 text-[12px] text-white/60">
                  <div className="flex justify-between"><span>NAV</span><span className="tabular-nums text-white/80">{fmtUsd(t.junior.navUsd)}</span></div>
                  <div className="flex justify-between"><span>LP price</span><span className="tabular-nums text-white/80">{t.junior.lpPrice?.toFixed(6) ?? '–'}</span></div>
                  <div className="flex justify-between"><span>Capacity used</span><span className="tabular-nums text-white/80">{fmtUsd(jrUsed)} / {fmtUsd(t.junior.maxCapacityUsd)}</span></div>
                  <CapacityBar used={jrUsed} total={t.junior.maxCapacityUsd ?? 0} />
                </div>
              </div>

              {/* Senior */}
              <div className="border-t border-white/[0.06] pt-4">
                <div className="flex items-baseline justify-between mb-3">
                  <div>
                    <div className="text-[10px] uppercase tracking-[0.18em] text-emerald-300/70">Senior · protected</div>
                    <div className="text-base text-white mt-1">sr{t.ticker}</div>
                  </div>
                  <div className="text-right">
                    <div className="text-2xl text-white tabular-nums">{fmtPct(t.senior.apy, 2)}</div>
                    <div className="text-[11px] text-white/40">{t.senior.pointsMult ? `${t.senior.pointsMult}× points` : ''}</div>
                  </div>
                </div>
                <div className="space-y-2 text-[12px] text-white/60">
                  <div className="flex justify-between"><span>NAV</span><span className="tabular-nums text-white/80">{fmtUsd(t.senior.navUsd)}</span></div>
                  <div className="flex justify-between"><span>LP price</span><span className="tabular-nums text-white/80">{t.senior.lpPrice?.toFixed(6) ?? '–'}</span></div>
                  <div className="flex justify-between"><span>Capacity used</span><span className="tabular-nums text-white/80">{fmtUsd(srUsed)} / {fmtUsd(t.senior.maxCapacityUsd)}</span></div>
                  <CapacityBar used={srUsed} total={t.senior.maxCapacityUsd ?? 0} />
                </div>
              </div>
            </div>

            {/* LP action activity (lifetime, since vault genesis) */}
            {t.lpActions && (t.lpActions['JR'] || t.lpActions['SR']) && (
              <div className="mt-6 pt-4 border-t border-white/[0.06]">
                <div className="text-[10px] uppercase tracking-[0.18em] text-white/35 mb-3">Activity (lifetime)</div>
                <div className="grid grid-cols-2 gap-x-10 text-[12px] text-white/60">
                  {(['JR','SR'] as const).map(leg => {
                    const a = t.lpActions?.[leg];
                    if (!a) return null;
                    return (
                      <div key={leg} className="space-y-1">
                        <div className="text-white/45">{leg === 'JR' ? 'Junior' : 'Senior'}</div>
                        <div className="flex justify-between"><span>Active LPs</span><span className="tabular-nums text-white/85">{a.uniqueWallets}</span></div>
                        <div className="flex justify-between"><span>Deposits</span><span className="tabular-nums text-emerald-300/80">{a.deposits}</span></div>
                        <div className="flex justify-between"><span>Withdrawals</span><span className="tabular-nums text-rose-300/80">{a.withdrawals}</span></div>
                      </div>
                    );
                  })}
                </div>
              </div>
            )}

            {/* Top LPs per leg */}
            {t.topLps && (t.topLps['JR']?.length || t.topLps['SR']?.length) && (
              <div className="mt-6 pt-4 border-t border-white/[0.06]">
                <div className="text-[10px] uppercase tracking-[0.18em] text-white/35 mb-3">Top LPs</div>
                <div className="grid grid-cols-1 md:grid-cols-2 gap-8">
                  {(['JR', 'SR'] as const).map(leg => {
                    const list = t.topLps?.[leg] || [];
                    if (!list.length) return null;
                    const lpPrice = leg === 'JR' ? t.junior.lpPrice : t.senior.lpPrice;
                    return (
                      <div key={leg}>
                        <div className={`text-[11px] mb-2 ${leg === 'JR' ? 'text-rose-300/70' : 'text-emerald-300/70'}`}>
                          {leg === 'JR' ? 'Junior' : 'Senior'}
                        </div>
                        <div className="space-y-1 text-[12px]">
                          {list.slice(0, 8).map((lp, i) => (
                            <div key={lp.wallet} className="flex items-center justify-between gap-3">
                              <div className="flex items-center gap-2">
                                <span className="text-white/30 tabular-nums w-4 text-right">{i + 1}</span>
                                <a href={`/wallet/?addr=${lp.wallet}`} className="font-mono text-white/65 hover:text-white">
                                  {shortAddr(lp.wallet)}
                                </a>
                              </div>
                              <div className="text-right tabular-nums">
                                <span className="text-white/80">{fmtUnits(lp.lpUnits)}</span>
                                {lpPrice && <span className="text-white/35 ml-2">{fmtUsd(lp.lpUnits * lpPrice)}</span>}
                              </div>
                            </div>
                          ))}
                        </div>
                      </div>
                    );
                  })}
                </div>
              </div>
            )}

            {/* Recent activity feed */}
            {t.recentActivity && t.recentActivity.length > 0 && (
              <div className="mt-6 pt-4 border-t border-white/[0.06]">
                <div className="flex items-baseline justify-between mb-3">
                  <div className="text-[10px] uppercase tracking-[0.18em] text-white/35">Recent activity</div>
                  <div className="text-[10px] text-white/35">last {t.recentActivity.length}</div>
                </div>
                <div className="space-y-1 text-[12px]">
                  {t.recentActivity.slice(0, 12).map(ev => {
                    const isDeposit = ev.lpUnits > 0;
                    return (
                      <div key={ev.sig} className="flex items-center justify-between gap-3">
                        <div className="flex items-center gap-2 min-w-0">
                          <span className={`text-[10px] uppercase tracking-wider ${ev.leg === 'JR' ? 'text-rose-300/70' : 'text-emerald-300/70'}`}>
                            {ev.leg}
                          </span>
                          <a href={`/wallet/?addr=${ev.wallet}`} className="font-mono text-white/65 hover:text-white">
                            {shortAddr(ev.wallet)}
                          </a>
                          <span className={isDeposit ? 'text-emerald-400/80' : 'text-rose-400/80'}>
                            {isDeposit ? 'deposit' : 'withdraw'}
                          </span>
                        </div>
                        <div className="text-right tabular-nums shrink-0">
                          <span className={isDeposit ? 'text-emerald-300/90' : 'text-rose-300/90'}>
                            {isDeposit ? '+' : ''}{fmtUnits(ev.lpUnits)}
                          </span>
                          <span className="text-white/30 ml-2 text-[10px]">{fmtTimeAgo(ev.blockTime)}</span>
                          <a href={`https://solscan.io/tx/${ev.sig}`} target="_blank" rel="noopener noreferrer"
                             className="text-white/30 hover:text-white ml-2 text-[10px]">↗</a>
                        </div>
                      </div>
                    );
                  })}
                </div>
              </div>
            )}

            {/* Return curve + what-if (slider drives the marker on the curve) */}
            {t.returnCurve && t.returnCurve.length > 0 && t.minCoverageRatio != null && (
              <CurveAndWhatIf
                curve={t.returnCurve}
                srSize={t.senior.effectiveNavUsd ?? 0}
                jrSize={t.junior.effectiveNavUsd ?? 0}
                underlyingApy={t.underlyingApy ?? 0.1178}
                minCoverage={t.minCoverageRatio}
                currentUtilization={
                  t.utilizationPct != null
                    ? t.utilizationPct / 100
                    : (t.coverageRatio && t.coverageRatio > 0 ? t.minCoverageRatio / t.coverageRatio : 0)
                }
                currentJrShare={t.currentJrReturnShare ?? 0}
              />
            )}

            {/* History plumbing — grows daily. Single-row state hidden until ≥2 days. */}
            {t.history && t.history.dates && t.history.dates.length >= 2 && (
              <div className="mt-6 pt-4 border-t border-white/[0.06]">
                <div className="text-[10px] uppercase tracking-[0.18em] text-white/35 mb-2">History ({t.history.dates.length} day{t.history.dates.length === 1 ? '' : 's'})</div>
                <div className="text-[11px] text-white/55">
                  Coverage range:&nbsp;
                  {fmtPct(Math.min(...t.history.coverageRatio.filter(v => v != null) as number[]), 2)}–
                  {fmtPct(Math.max(...t.history.coverageRatio.filter(v => v != null) as number[]), 2)}
                </div>
              </div>
            )}

            {/* On-chain refs */}
            <div className="mt-5 pt-4 border-t border-white/[0.06] text-[11px] text-white/35 flex flex-wrap gap-x-6 gap-y-1">
              <span>Vault <a href={`https://solscan.io/account/${t.address}`} target="_blank" rel="noopener noreferrer" className="font-mono text-white/55 hover:text-white">{t.address.slice(0,4)}…{t.address.slice(-4)}</a></span>
              {t.junior.lpMint && <span>jr mint <a href={`https://solscan.io/token/${t.junior.lpMint}`} target="_blank" rel="noopener noreferrer" className="font-mono text-white/55 hover:text-white">{t.junior.lpMint.slice(0,4)}…{t.junior.lpMint.slice(-4)}</a></span>}
              {t.senior.lpMint && <span>sr mint <a href={`https://solscan.io/token/${t.senior.lpMint}`} target="_blank" rel="noopener noreferrer" className="font-mono text-white/55 hover:text-white">{t.senior.lpMint.slice(0,4)}…{t.senior.lpMint.slice(-4)}</a></span>}
              {t.seedId && <span>seed <span className="font-mono text-white/55">{t.seedId}</span></span>}
            </div>
          </section>
        );
      })}

      <div className="text-[11px] text-white/25 pt-4 border-t border-white/[0.06]">
        Source: {data.meta.source}
      </div>
    </div>
  );
}
