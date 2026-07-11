'use client';
import Link from 'next/link';
import { useEffect, useMemo, useState } from 'react';

type FlowsSummary = {
  totalDeposits: number;
  totalQueues: number;
  totalExecutes: number;
  totalFastWithdraws: number;
  uniqueActors: number;
};

type ActivityEvent = {
  sig: string;
  blockTime: number;
  actor: string;
  ixName: string;
  lpDelta: number | null;
  baseDelta: number | null;
  argsJson: string | null;
};

type TopHolder = { wallet: string; lpAmount: number; lpUi: number };

type VaultHistory = {
  dates: string[];
  aumUi: (number | null)[];
  lpBalance: (number | null)[];
  navPerShare: (number | null)[];
  apy7d: (number | null)[];
  apy30d: (number | null)[];
  netFlowLp: (number | null)[];
  uniqueActors: (number | null)[];
};

type Vault = {
  address: string;
  name?: string | null;
  isManaged?: boolean;
  strategist?: string | null;
  underlyingMint: string;
  underlyingSymbol: string | null;
  underlyingDecimals: number | null;
  lpDecimals?: number | null;
  mintLp: string;
  squadsVault: string | null;
  aumInBase: number | null;
  aumUi: number | null;
  aumUsd: number | null;
  lpBalance: number | null;
  navPerShare: number | null;
  pctFull: number | null;
  deployedRatio: number | null;
  idleRatio: number | null;
  fastExitUtilization: number | null;
  pendingWithdrawRatio: number | null;
  managementFeeBps: number | null;
  normalWithdrawalCutBp: number | null;
  fastWithdrawalCutBp: number | null;
  snapshotDate: string;
  history?: VaultHistory;
  topHolders?: TopHolder[];
  recentActivity?: ActivityEvent[];
  flows?: FlowsSummary;
};

type StrategyVaultData = {
  meta: { generatedAt: string; source: string; count: number };
  vaults: Vault[];
};

function shortAddr(a: string) {
  return `${a.slice(0, 4)}…${a.slice(-4)}`;
}

function fmtUnits(n: number | null | undefined, decimals = 2) {
  if (n == null) return '–';
  if (Math.abs(n) >= 1e9) return `${(n / 1e9).toFixed(2)}B`;
  if (Math.abs(n) >= 1e6) return `${(n / 1e6).toFixed(2)}M`;
  if (Math.abs(n) >= 1e3) return `${(n / 1e3).toFixed(1)}K`;
  return n.toFixed(decimals);
}

function fmtUsd(n: number | null | undefined) {
  if (n == null) return '–';
  if (Math.abs(n) >= 1e9) return `$${(n / 1e9).toFixed(2)}B`;
  if (Math.abs(n) >= 1e6) return `$${(n / 1e6).toFixed(2)}M`;
  if (Math.abs(n) >= 1e3) return `$${(n / 1e3).toFixed(1)}K`;
  return `$${n.toFixed(0)}`;
}

// AUM filter thresholds (USD). Default keeps the meaningful vaults visible.
const AUM_TIERS: { label: string; min: number }[] = [
  { label: 'All',    min: 0 },
  { label: '>$100K', min: 100_000 },
  { label: '>$1M',   min: 1_000_000 },
  { label: '>$10M',  min: 10_000_000 },
];

function fmtPct(n: number | null | undefined, digits = 2) {
  if (n == null) return '–';
  return `${(n * 100).toFixed(digits)}%`;
}

function fmtBp(n: number | null | undefined) {
  if (n == null) return '–';
  return `${(n / 100).toFixed(2)}%`;
}

function Sparkline({ values, height = 24, width = 80 }: {
  values: (number | null)[]; height?: number; width?: number;
}) {
  const clean = values.filter((v): v is number => v != null && isFinite(v));
  if (clean.length < 2) return <span className="text-white/25 text-[10px]">–</span>;
  const min = Math.min(...clean);
  const max = Math.max(...clean);
  const range = max - min || 1;
  const step = width / (clean.length - 1);
  const points = clean.map((v, i) => {
    const x = i * step;
    const y = height - ((v - min) / range) * height;
    return `${x.toFixed(1)},${y.toFixed(1)}`;
  }).join(' ');
  return (
    <svg width={width} height={height} className="overflow-visible">
      <polyline points={points} fill="none" stroke="rgba(255,255,255,0.55)" strokeWidth={1.2} />
    </svg>
  );
}

function VaultCard({ vault }: { vault: Vault }) {
  const sym = vault.underlyingSymbol ?? '?';

  return (
    <Link
      href={`/strategy/?addr=${vault.address}`}
      className="grid grid-cols-12 gap-3 items-center border-t border-white/[0.06] py-4 hover:bg-white/[0.015] transition"
    >
      <div className="col-span-3 min-w-0">
        <div className="flex items-center gap-2">
          <span className="text-[12px] text-white/85 truncate">{vault.name ?? 'Unnamed'}</span>
          <span className="text-[10px] text-white/35 border border-white/10 rounded px-1.5 py-px shrink-0">
            {sym}
          </span>
        </div>
        <div className="font-mono text-[10px] text-white/30 mt-0.5 truncate">{shortAddr(vault.address)}</div>
      </div>

      {/* AUM keeps its subtext (native token amount); other columns rely on the header. */}
      <div className="col-span-2 text-right">
        <div className="text-white tabular-nums text-[13px]">
          {vault.aumUsd != null ? fmtUsd(vault.aumUsd) : (vault.aumUi != null ? `${fmtUnits(vault.aumUi)} ${sym}` : '–')}
        </div>
        <div className="text-[10px] text-white/35">
          {vault.aumUi != null ? `${fmtUnits(vault.aumUi)} ${sym}` : '–'}
        </div>
      </div>

      <div className="col-span-2 text-right text-white/75 tabular-nums text-[12px]">
        {vault.navPerShare != null ? vault.navPerShare.toFixed(6) : '–'}
      </div>

      <div className="col-span-1 text-right text-white/75 tabular-nums text-[12px]">
        {fmtPct(vault.deployedRatio, 1)}
      </div>

      <div className="col-span-1 text-right text-white/75 tabular-nums text-[12px]">
        {fmtBp(vault.managementFeeBps)}
      </div>

      <div className="col-span-1 text-right text-white/75 tabular-nums text-[12px]">
        {vault.flows?.uniqueActors ?? 0}
      </div>

      <div className="col-span-2 flex justify-end items-center">
        {vault.history && vault.history.aumUi.length > 1 ? (
          <Sparkline values={vault.history.aumUi} width={80} height={22} />
        ) : (
          <span className="text-white/25 text-[10px]">history building</span>
        )}
      </div>
    </Link>
  );
}

export function StrategyVaultAnalytics() {
  const [data, setData] = useState<StrategyVaultData | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const [minAum, setMinAum] = useState(1_000_000); // default: >$1M

  useEffect(() => {
    fetch('/strategy_vault.json')
      .then(r => (r.ok ? r.json() : Promise.reject(`HTTP ${r.status}`)))
      .then(setData)
      .catch(e => setErr(String(e)));
  }, []);

  const aggregates = useMemo(() => {
    if (!data?.vaults) return null;
    let totalAumUsd = 0, activeCount = 0, managedCount = 0, depositors = 0;
    for (const v of data.vaults) {
      totalAumUsd += v.aumUsd ?? 0;
      if ((v.aumInBase ?? 0) > 0) activeCount += 1;
      if (v.isManaged) managedCount += 1;
      depositors += v.flows?.uniqueActors ?? 0;
    }
    return { totalAumUsd, activeCount, managedCount, depositors, total: data.vaults.length };
  }, [data]);

  const filtered = useMemo(() => {
    if (!data?.vaults) return [];
    return data.vaults
      .filter(v => (v.aumUsd ?? 0) >= minAum)
      .sort((a, b) => (b.aumUsd ?? 0) - (a.aumUsd ?? 0));
  }, [data, minAum]);

  if (err) return <div className="text-sm text-rose-400/80">Failed to load strategy vault data: {err}</div>;
  if (!data) return <div className="text-sm text-white/40">Loading…</div>;

  if (!data.vaults.length) {
    return (
      <div className="border-t border-b border-white/[0.06] py-12 text-center">
        <p className="text-sm text-white/40">No strategy vaults indexed yet.</p>
      </div>
    );
  }

  return (
    <div className="space-y-6">
      {/* Hero stats */}
      <div className="grid grid-cols-2 md:grid-cols-4 gap-x-10 gap-y-4 border-t border-b border-white/[0.06] py-5">
        <div>
          <div className="text-[10px] uppercase tracking-[0.18em] text-white/35">Total AUM</div>
          <div className="text-3xl text-white tabular-nums mt-1">{fmtUsd(aggregates?.totalAumUsd)}</div>
          <div className="text-[11px] text-white/40 mt-0.5">across all vaults</div>
        </div>
        <div>
          <div className="text-[10px] uppercase tracking-[0.18em] text-white/35">Managed strategies</div>
          <div className="text-3xl text-white tabular-nums mt-1">{aggregates?.managedCount ?? 0}</div>
          <div className="text-[11px] text-white/40 mt-0.5">{aggregates?.activeCount ?? 0} active vaults</div>
        </div>
        <div>
          <div className="text-[10px] uppercase tracking-[0.18em] text-white/35">Depositors</div>
          <div className="text-3xl text-white tabular-nums mt-1">{fmtUnits(aggregates?.depositors, 0)}</div>
          <div className="text-[11px] text-white/40 mt-0.5">unique, all-time</div>
        </div>
        <div>
          <div className="text-[10px] uppercase tracking-[0.18em] text-white/35">Vaults tracked</div>
          <div className="text-3xl text-white tabular-nums mt-1">{aggregates?.total ?? 0}</div>
          <div className="text-[11px] text-white/40 mt-0.5">on-chain (incl. empty)</div>
        </div>
      </div>

      {/* AUM filter */}
      <div className="flex items-center justify-between gap-3 text-[11px]">
        <span className="text-white/40">{filtered.length} shown</span>
        <div className="flex items-center gap-1.5">
          <span className="text-white/35 mr-1">Min AUM</span>
          {AUM_TIERS.map(t => (
            <button
              key={t.label}
              onClick={() => setMinAum(t.min)}
              className={`rounded px-2 py-0.5 border transition ${
                minAum === t.min
                  ? 'bg-white/10 border-white/20 text-white'
                  : 'border-white/10 text-white/40 hover:text-white/80'
              }`}
            >
              {t.label}
            </button>
          ))}
        </div>
      </div>

      {/* Column headers */}
      <div className="grid grid-cols-12 gap-3 text-[10px] uppercase tracking-[0.18em] text-white/30 pb-1 border-b border-white/[0.06]">
        <div className="col-span-3">Vault</div>
        <div className="col-span-2 text-right">AUM</div>
        <div className="col-span-2 text-right">NAV/share</div>
        <div className="col-span-1 text-right">Deploy</div>
        <div className="col-span-1 text-right">Mgmt fee</div>
        <div className="col-span-1 text-right">LPs</div>
        <div className="col-span-2 text-right">AUM history</div>
      </div>

      {/* Vault list */}
      <div>
        {filtered.map(v => <VaultCard key={v.address} vault={v} />)}
      </div>

      {/* Footer */}
      <div className="text-[11px] text-white/25 pt-4 border-t border-white/[0.06]">
        Source: {data.meta.source}
      </div>
    </div>
  );
}
