'use client';
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
  underlyingMint: string;
  underlyingSymbol: string | null;
  underlyingDecimals: number | null;
  lpDecimals?: number | null;
  mintLp: string;
  squadsVault: string | null;
  aumInBase: number | null;
  aumUi: number | null;
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

function fmtPct(n: number | null | undefined, digits = 2) {
  if (n == null) return '–';
  return `${(n * 100).toFixed(digits)}%`;
}

function fmtBp(n: number | null | undefined) {
  if (n == null) return '–';
  return `${(n / 100).toFixed(2)}%`;
}

function fmtTimeAgo(ts: number): string {
  const sec = Math.max(0, Math.floor(Date.now() / 1000) - ts);
  if (sec < 60) return `${sec}s ago`;
  if (sec < 3600) return `${Math.floor(sec / 60)}m ago`;
  if (sec < 86400) return `${Math.floor(sec / 3600)}h ago`;
  return `${Math.floor(sec / 86400)}d ago`;
}

function ActionColor(ixName: string): string {
  if (ixName === 'deposit_liquidity') return 'text-emerald-400/80';
  if (ixName.startsWith('execute_') || ixName === 'queue_withdrawal') return 'text-rose-400/80';
  if (ixName === 'fill_withdrawal') return 'text-amber-400/70';
  if (ixName === 'cancel_withdrawal') return 'text-white/45';
  return 'text-white/45';
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

function parseArgs(argsJson: string | null): Record<string, number> | null {
  if (!argsJson) return null;
  try { return JSON.parse(argsJson); } catch { return null; }
}

function VaultCard({ vault }: { vault: Vault }) {
  const [expanded, setExpanded] = useState(false);
  const sym = vault.underlyingSymbol ?? '?';

  return (
    <div className="border-t border-white/[0.06] pt-4 pb-2">
      <div className="grid grid-cols-12 gap-3 items-center cursor-pointer"
           onClick={() => setExpanded(!expanded)}>
        <div className="col-span-3">
          <div className="flex items-center gap-2">
            <span className="text-white/25 text-[10px] tabular-nums w-3">{expanded ? '−' : '+'}</span>
            <a href={`https://solscan.io/account/${vault.address}`}
               onClick={e => e.stopPropagation()}
               target="_blank" rel="noopener noreferrer"
               className="font-mono text-white/70 hover:text-white text-[12px]">
              {shortAddr(vault.address)}
            </a>
            <span className="text-[10px] text-white/35 border border-white/10 rounded px-1.5 py-px">
              {sym}
            </span>
            {vault.name && (
              <span className="text-[11px] text-white/60 truncate">{vault.name}</span>
            )}
          </div>
          {vault.squadsVault && (
            <div className="text-[10px] text-white/30 ml-5 mt-0.5">
              squads {shortAddr(vault.squadsVault)}
            </div>
          )}
        </div>

        <div className="col-span-2 text-right">
          <div className="text-white tabular-nums text-[13px]">
            {vault.aumUi != null ? `${fmtUnits(vault.aumUi)} ${sym}` : '–'}
          </div>
          <div className="text-[10px] text-white/35">
            {vault.pctFull != null && vault.pctFull > 0 ? `${fmtPct(vault.pctFull, 1)} full` : 'uncapped'}
          </div>
        </div>

        <div className="col-span-2 text-right">
          <div className="text-white/75 tabular-nums text-[12px]">
            {vault.navPerShare != null ? vault.navPerShare.toFixed(6) : '–'}
          </div>
          <div className="text-[10px] text-white/35">nav/share</div>
        </div>

        <div className="col-span-1 text-right">
          <div className="text-white/75 tabular-nums text-[12px]">
            {fmtPct(vault.deployedRatio, 1)}
          </div>
          <div className="text-[10px] text-white/35">deployed</div>
        </div>

        <div className="col-span-1 text-right">
          <div className="text-white/75 tabular-nums text-[12px]">
            {fmtBp(vault.managementFeeBps)}
          </div>
          <div className="text-[10px] text-white/35">mgmt fee</div>
        </div>

        <div className="col-span-1 text-right">
          <div className="text-white/75 tabular-nums text-[12px]">
            {vault.flows?.uniqueActors ?? 0}
          </div>
          <div className="text-[10px] text-white/35">LPs</div>
        </div>

        <div className="col-span-2 flex justify-end items-center">
          {vault.history && vault.history.aumUi.length > 1 ? (
            <Sparkline values={vault.history.aumUi} width={80} height={22} />
          ) : (
            <span className="text-white/25 text-[10px]">history building</span>
          )}
        </div>
      </div>

      {expanded && (
        <div className="mt-4 grid grid-cols-1 md:grid-cols-3 gap-6 pl-6 pb-2">
          {/* State */}
          <div>
            <div className="text-[10px] uppercase tracking-[0.18em] text-white/35 mb-2">State</div>
            <div className="space-y-1.5 text-[11px]">
              <Row label="AUM" v={vault.aumUi != null ? `${fmtUnits(vault.aumUi, 4)} ${sym}` : '–'} />
              <Row label="LP supply" v={fmtUnits(vault.lpBalance)} />
              <Row label="Idle ratio" v={fmtPct(vault.idleRatio, 1)} />
              <Row label="Pending withdraw" v={fmtPct(vault.pendingWithdrawRatio, 2)} />
              <Row label="Fast-exit used" v={fmtPct(vault.fastExitUtilization, 1)} />
              <Row label="Normal exit fee" v={fmtBp(vault.normalWithdrawalCutBp)} />
              <Row label="Fast exit fee" v={fmtBp(vault.fastWithdrawalCutBp)} />
              <Row label="Snapshot" v={vault.snapshotDate} muted />
            </div>
          </div>

          {/* Top holders */}
          <div>
            <div className="text-[10px] uppercase tracking-[0.18em] text-white/35 mb-2">
              Top LPs {vault.topHolders && vault.topHolders.length > 0 && `(${vault.topHolders.length})`}
            </div>
            {vault.topHolders && vault.topHolders.length > 0 ? (
              <div className="space-y-1 text-[11px]">
                {vault.topHolders.slice(0, 8).map((h, i) => (
                  <div key={h.wallet} className="flex justify-between">
                    <div className="flex items-center gap-2">
                      <span className="text-white/30 w-3 text-right tabular-nums">{i + 1}</span>
                      <a href={`/wallet/?addr=${h.wallet}`}
                         className="font-mono text-white/65 hover:text-white">
                        {shortAddr(h.wallet)}
                      </a>
                    </div>
                    <span className="text-white/75 tabular-nums">{fmtUnits(h.lpUi ?? h.lpAmount)}</span>
                  </div>
                ))}
              </div>
            ) : (
              <div className="text-[11px] text-white/30">No LPs indexed.</div>
            )}
          </div>

          {/* Recent activity */}
          <div>
            <div className="text-[10px] uppercase tracking-[0.18em] text-white/35 mb-2">
              Recent activity {vault.recentActivity && vault.recentActivity.length > 0 && `(${vault.recentActivity.length})`}
            </div>
            {vault.recentActivity && vault.recentActivity.length > 0 ? (
              <div className="space-y-1 text-[11px]">
                {vault.recentActivity.slice(0, 10).map((ev, i) => {
                  const args = parseArgs(ev.argsJson);
                  const uDec = vault.underlyingDecimals ?? 9;
                  const lpDec = vault.lpDecimals ?? uDec;
                  // deposit amounts are base atoms; queue/fast-withdraw LP atoms
                  const size = args?.depositTokenAmountIn != null
                    ? args.depositTokenAmountIn / 10 ** uDec
                    : args?.queueLpAmount != null
                      ? args.queueLpAmount / 10 ** lpDec
                      : args?.fastWithdrawLpAmount != null
                        ? args.fastWithdrawLpAmount / 10 ** lpDec
                        : null;
                  return (
                    <div key={`${ev.sig}-${i}`} className="flex justify-between gap-2">
                      <div className="flex items-center gap-2 min-w-0">
                        <a href={`/wallet/?addr=${ev.actor}`}
                           className="font-mono text-white/60 hover:text-white shrink-0">
                          {shortAddr(ev.actor)}
                        </a>
                        <span className={`${ActionColor(ev.ixName)} truncate`}>{ev.ixName}</span>
                      </div>
                      <div className="flex items-center gap-2 shrink-0">
                        {size != null && (
                          <span className="text-white/50 tabular-nums text-[10px]">{fmtUnits(size)}</span>
                        )}
                        <span className="text-white/30 text-[10px]">{fmtTimeAgo(ev.blockTime)}</span>
                        <a href={`https://solscan.io/tx/${ev.sig}`}
                           target="_blank" rel="noopener noreferrer"
                           className="text-white/30 hover:text-white text-[10px]">↗</a>
                      </div>
                    </div>
                  );
                })}
              </div>
            ) : (
              <div className="text-[11px] text-white/30">No activity indexed yet.</div>
            )}

            {vault.flows && (
              <div className="mt-3 pt-2 border-t border-white/[0.06] text-[10px] text-white/45 flex gap-3 flex-wrap">
                <span>{vault.flows.totalDeposits} deps</span>
                <span>{vault.flows.totalQueues} queued</span>
                <span>{vault.flows.totalExecutes} exec</span>
                <span>{vault.flows.totalFastWithdraws} fast</span>
              </div>
            )}
          </div>
        </div>
      )}
    </div>
  );
}

function Row({ label, v, muted }: { label: string; v: string; muted?: boolean }) {
  return (
    <div className="flex justify-between">
      <span className="text-white/45">{label}</span>
      <span className={`tabular-nums ${muted ? 'text-white/45' : 'text-white/80'}`}>{v}</span>
    </div>
  );
}

export function StrategyVaultAnalytics() {
  const [data, setData] = useState<StrategyVaultData | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const [onlyActive, setOnlyActive] = useState(true);

  useEffect(() => {
    fetch('/strategy_vault.json')
      .then(r => (r.ok ? r.json() : Promise.reject(`HTTP ${r.status}`)))
      .then(setData)
      .catch(e => setErr(String(e)));
  }, []);

  const aggregates = useMemo(() => {
    if (!data?.vaults) return null;
    const bySym: Record<string, { count: number; aumUi: number }> = {};
    let activeCount = 0;
    let totalActors = 0;
    for (const v of data.vaults) {
      const sym = v.underlyingSymbol ?? '?';
      bySym[sym] = bySym[sym] ?? { count: 0, aumUi: 0 };
      bySym[sym].count += 1;
      if (v.aumUi != null) bySym[sym].aumUi += v.aumUi;
      if ((v.aumInBase ?? 0) > 0) activeCount += 1;
      totalActors += v.flows?.uniqueActors ?? 0;
    }
    return { bySym, activeCount, totalActors, total: data.vaults.length };
  }, [data]);

  const filtered = useMemo(() => {
    if (!data?.vaults) return [];
    const list = onlyActive
      ? data.vaults.filter(v => (v.aumInBase ?? 0) > 0 || (v.recentActivity?.length ?? 0) > 0)
      : data.vaults;
    return [...list].sort((a, b) => (b.aumInBase ?? 0) - (a.aumInBase ?? 0));
  }, [data, onlyActive]);

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
      {/* Header aggregates */}
      <div className="grid grid-cols-2 md:grid-cols-4 gap-x-10 gap-y-4 border-t border-b border-white/[0.06] py-5">
        <div>
          <div className="text-[10px] uppercase tracking-[0.18em] text-white/35">Vaults</div>
          <div className="text-2xl text-white tabular-nums mt-1">{aggregates?.activeCount ?? 0}</div>
          <div className="text-[11px] text-white/40 mt-0.5">
            active of {aggregates?.total ?? 0} total
          </div>
        </div>
        {aggregates && Object.entries(aggregates.bySym).slice(0, 3).map(([sym, v]) => (
          <div key={sym}>
            <div className="text-[10px] uppercase tracking-[0.18em] text-white/35">{sym}</div>
            <div className="text-2xl text-white tabular-nums mt-1">{fmtUnits(v.aumUi)}</div>
            <div className="text-[11px] text-white/40 mt-0.5">{v.count} vaults</div>
          </div>
        ))}
      </div>

      {/* Filter toggle */}
      <div className="flex items-center gap-3 text-[11px] text-white/50">
        <span>{filtered.length} vaults shown</span>
        <button
          onClick={() => setOnlyActive(!onlyActive)}
          className="text-white/40 hover:text-white/80 border border-white/10 rounded px-2 py-0.5 transition"
        >
          {onlyActive ? 'active only · click to show all' : 'showing all · click to hide idle'}
        </button>
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
