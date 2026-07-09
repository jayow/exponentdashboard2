'use client';
import { Suspense, useEffect, useMemo, useState } from 'react';
import Link from 'next/link';
import { useSearchParams } from 'next/navigation';
import {
  ActivityEvent, Proposal, StrategyTxnShard, StrategyVaultData, Vault, VaultOpGroup,
  actionColor, activitySize, actorRole, daysUntil, executionTypeColor, fmtBp,
  fmtDate, fmtPct, fmtTimeAgo, fmtUnits, inceptionApy, lastNonNull,
  proposalStatusColor, shortAddr, vaultOpDetail, vaultOpGroup,
} from '@/lib/strategy-vault';

function LineChart({ dates, values, height = 64, label }: {
  dates: string[]; values: (number | null)[]; height?: number; label: string;
}) {
  const pts = dates
    .map((d, i) => ({ d, v: values[i] }))
    .filter((p): p is { d: string; v: number } => p.v != null && isFinite(p.v as number));
  if (pts.length < 2) {
    return (
      <div>
        <div className="text-[10px] uppercase tracking-[0.18em] text-white/35 mb-1">{label}</div>
        <div className="text-[11px] text-white/25 h-[64px] flex items-center">history building</div>
      </div>
    );
  }
  const width = 320;
  const min = Math.min(...pts.map(p => p.v));
  const max = Math.max(...pts.map(p => p.v));
  const range = max - min || 1;
  const step = width / (pts.length - 1);
  const points = pts.map((p, i) => {
    const x = i * step;
    const y = height - 6 - ((p.v - min) / range) * (height - 12);
    return `${x.toFixed(1)},${y.toFixed(1)}`;
  }).join(' ');
  return (
    <div>
      <div className="text-[10px] uppercase tracking-[0.18em] text-white/35 mb-1">{label}</div>
      <svg width="100%" height={height} viewBox={`0 0 ${width} ${height}`} preserveAspectRatio="none" className="overflow-visible">
        <polyline points={points} fill="none" stroke="rgba(255,255,255,0.55)" strokeWidth={1.2} vectorEffect="non-scaling-stroke" />
      </svg>
      <div className="flex justify-between text-[9px] text-white/25 mt-0.5">
        <span>{pts[0].d}</span>
        <span>{pts[pts.length - 1].d}</span>
      </div>
    </div>
  );
}

function Stat({ label, value, sub }: { label: string; value: string; sub?: string }) {
  return (
    <div>
      <div className="text-[10px] uppercase tracking-[0.18em] text-white/35">{label}</div>
      <div className="text-lg text-white tabular-nums mt-0.5">{value}</div>
      {sub && <div className="text-[10px] text-white/35 mt-0.5">{sub}</div>}
    </div>
  );
}

function Row({ label, v }: { label: string; v: string }) {
  return (
    <div className="flex justify-between">
      <span className="text-white/45">{label}</span>
      <span className="tabular-nums text-white/80">{v}</span>
    </div>
  );
}

function SectionTitle({ children }: { children: React.ReactNode }) {
  return <div className="text-[10px] uppercase tracking-[0.18em] text-white/35 mb-2">{children}</div>;
}

function ProposalRow({ p, threshold }: { p: Proposal; threshold: number | null | undefined }) {
  const [open, setOpen] = useState(false);
  const rejectPct = p.lpSnapshot > 0 ? p.rejectVotes / p.lpSnapshot : 0;
  const when = p.status === 'Executed'
    ? `Executed ${fmtTimeAgo(p.executableAt || p.createdAt)}`
    : p.status === 'Active'
      ? `Voting ends ${fmtDate(p.votingEndsAt)}`
      : `Created ${fmtTimeAgo(p.createdAt)}`;
  return (
    <div className="border-b border-white/[0.04]">
      <div className="grid grid-cols-12 gap-2 items-center py-2 cursor-pointer"
           onClick={() => setOpen(!open)}>
        <span className="col-span-3 md:col-span-2 text-white/80">
          <span className="text-white/25 mr-1.5">{open ? '−' : '+'}</span>
          Proposal #{p.id}
        </span>
        <span className="col-span-3 md:col-span-2">
          <span className={`text-[10px] rounded px-1.5 py-0.5 ${proposalStatusColor(p.status)}`}>
            {p.status}
          </span>
        </span>
        <span className="col-span-3 md:col-span-3 text-white/45 text-[11px]">{when}</span>
        <span className="col-span-2 md:col-span-3 text-right text-white/55 tabular-nums text-[11px]">
          {fmtPct(rejectPct, 1)}{threshold != null ? ` / ${fmtPct(threshold, 0)}` : ''}
          <span className="text-white/30 text-[10px] ml-1 hidden md:inline">reject</span>
        </span>
        <span className="col-span-1 md:col-span-2 text-right text-white/35 text-[11px]">
          {p.actions.length > 0 ? `${p.actions.length} action${p.actions.length > 1 ? 's' : ''}` : ''}
        </span>
      </div>
      {open && (
        <div className="pb-3 pl-6 space-y-1 text-[11px]">
          {p.actions.length === 0 && (
            <div className="text-white/30">
              {p.parseError ? 'Actions use an older encoding — not decoded.' : 'No actions.'}
            </div>
          )}
          {p.actions.map((a, i) => (
            <div key={i} className="flex justify-between gap-4 flex-wrap">
              <span className="text-white/70">
                {a.name}
                {a.mode && a.name !== `${a.mode} Policy` && (
                  <span className="text-white/40"> · {a.mode}</span>
                )}
              </span>
              <span className="text-white/50 text-right">
                {[a.assets.join(', '), a.verbs.join(' / '), a.protocols.join(', ')]
                  .filter(Boolean).join(' · ')}
              </span>
            </div>
          ))}
          <div className="text-white/30 text-[10px] pt-1">
            proposed {fmtDate(p.createdAt)} by <span className="font-mono">{shortAddr(p.proposer)}</span>
          </div>
        </div>
      )}
    </div>
  );
}

type TxFilter = 'all' | 'deposits' | 'withdrawals';
type TxTab = 'user' | 'activity' | 'governance';
type OpFilter = 'all' | VaultOpGroup;

const OP_FILTERS: { key: OpFilter; label: string }[] = [
  { key: 'all',       label: 'All' },
  { key: 'proposals', label: 'Proposals' },
  { key: 'fills',     label: 'Withdrawal fills' },
  { key: 'position',  label: 'Position updates' },
  { key: 'policies',  label: 'Policies' },
  { key: 'settings',  label: 'Settings' },
];

const PAGE = 50;

function StrategyDetail({ vault }: { vault: Vault }) {
  const [txTab, setTxTab] = useState<TxTab>('user');
  const [txFilter, setTxFilter] = useState<TxFilter>('all');
  const [opFilter, setOpFilter] = useState<OpFilter>('all');
  const [shown, setShown] = useState(PAGE);
  const [shard, setShard] = useState<StrategyTxnShard | null>(null);

  // Full-history shard; strategy_vault.json's capped window is the fallback.
  useEffect(() => {
    fetch(`/strategy-txns/${vault.address}.json`)
      .then(r => (r.ok ? r.json() : null))
      .then(setShard)
      .catch(() => setShard(null));
  }, [vault.address]);
  const sym = vault.underlyingSymbol ?? '?';
  const uDec = vault.underlyingDecimals ?? 9;
  const lpDec = vault.lpDecimals ?? uDec;
  const apy7d = vault.exponentApy?.d7 ?? lastNonNull(vault.history?.apy7d);
  const apy30d = vault.exponentApy?.d30 ?? lastNonNull(vault.history?.apy30d);
  const implied = vault.impliedNav;
  const lifeApy = implied ? inceptionApy(implied.dates, implied.nav) : null;
  const tokens = (vault.positions?.tokens ?? []).filter(t => t.amountUi > 0);
  const obligations = vault.positions?.obligations ?? [];

  // fill_withdrawal is manager-executed — it lives in the Vault ops tab.
  const userTxs = useMemo(
    () => shard?.userActivity
      ?? (vault.recentActivity ?? []).filter(ev => ev.ixName !== 'fill_withdrawal'),
    [shard, vault.recentActivity],
  );
  const vaultTxs = useMemo(
    () => shard?.vaultActivity ?? vault.managerActivity ?? [],
    [shard, vault.managerActivity],
  );
  const roles = shard?.roles ?? null;

  const executions = shard?.executions ?? [];

  const txs = useMemo(() => {
    let list: ActivityEvent[];
    if (txTab === 'governance') {
      list = opFilter === 'all'
        ? vaultTxs
        : vaultTxs.filter(ev => vaultOpGroup(ev.ixName) === opFilter);
    } else if (txFilter === 'deposits') {
      list = userTxs.filter(ev => ev.ixName === 'deposit_liquidity');
    } else if (txFilter === 'withdrawals') {
      list = userTxs.filter(ev =>
        ev.ixName.includes('withdrawal') || ev.ixName === 'queue_withdrawal');
    } else {
      list = userTxs;
    }
    return list;
  }, [userTxs, vaultTxs, txTab, txFilter, opFilter]);

  // Reset pagination when the visible list changes.
  useEffect(() => { setShown(PAGE); }, [txTab, txFilter, opFilter]);

  return (
    <div className="space-y-8">
      {/* Header */}
      <div className="flex items-start justify-between flex-wrap gap-3">
        <div className="min-w-0">
          <div className="flex items-center gap-2.5 flex-wrap">
            <h1 className="text-white text-xl">{vault.name ?? shortAddr(vault.address)}</h1>
            <span className="text-[10px] text-white/35 border border-white/10 rounded px-1.5 py-px">{sym}</span>
            {vault.depositTicker && vault.depositTicker !== sym && (
              <span className="text-[10px] text-white/35 border border-white/10 rounded px-1.5 py-px">
                deposits in {vault.depositTicker}
              </span>
            )}
            {vault.profile && (
              <span className="text-[10px] text-white/45 border border-white/10 rounded px-1.5 py-px capitalize">
                {vault.profile}
              </span>
            )}
          </div>
          <div className="flex items-center gap-3 mt-1.5 text-[11px] flex-wrap">
            {vault.strategist && <span className="text-white/50">by {vault.strategist}</span>}
            <a href={`https://solscan.io/account/${vault.address}`}
               target="_blank" rel="noopener noreferrer"
               className="font-mono text-white/40 hover:text-white">
              {shortAddr(vault.address)}
            </a>
            {vault.squadsVault && (
              <span className="text-white/30">squads {shortAddr(vault.squadsVault)}</span>
            )}
          </div>
          {vault.description && (
            <p className="text-[11px] text-white/40 mt-2 max-w-3xl whitespace-pre-line">
              {vault.description.split('\n\n')[0]}
            </p>
          )}
        </div>
        <a href={`https://app.exponent.finance/en/strategy/${vault.address}?type=managed`}
           target="_blank" rel="noopener noreferrer"
           className="text-[11px] text-white/40 hover:text-white border border-white/10 rounded px-2 py-1 transition shrink-0">
          View on Exponent ↗
        </a>
      </div>

      {/* Headline stats */}
      <div className="grid grid-cols-2 sm:grid-cols-3 lg:grid-cols-6 gap-x-8 gap-y-4 border-t border-b border-white/[0.06] py-5">
        <Stat label="AUM" value={`${fmtUnits(vault.aumUi)} ${sym}`}
              sub={vault.capacityUi
                ? `cap ${fmtUnits(vault.capacityUi)} ${sym} · ${fmtPct(vault.pctFull, 0)} full`
                : 'uncapped'} />
        <Stat label="NAV / share" value={vault.navPerShare != null ? vault.navPerShare.toFixed(4) : '–'} />
        <Stat label="APY 7d" value={fmtPct(apy7d)} sub="realized, from NAV" />
        <Stat label="APY 30d" value={fmtPct(apy30d)} sub="realized, from NAV" />
        <Stat label="Deployed" value={fmtPct(vault.deployedRatio, 1)}
              sub={`${fmtUnits(vault.deployedUi)} ${sym} in positions`} />
        <Stat label="Depositors" value={String(vault.flows?.uniqueActors ?? 0)}
              sub={`${vault.flows?.totalDeposits ?? 0} deposits`} />
      </div>

      {/* Charts + terms */}
      <div className="grid grid-cols-1 md:grid-cols-3 gap-8">
        <LineChart
          label="NAV / share (deposit-implied)"
          dates={implied?.dates ?? []}
          values={implied?.nav ?? []}
        />
        <LineChart
          label={`AUM (${sym}, daily snapshots)`}
          dates={vault.history?.dates ?? []}
          values={vault.history?.aumUi ?? []}
        />
        <div className="space-y-1.5 text-[11px]">
          <SectionTitle>Vault terms</SectionTitle>
          <Row label="Capacity" v={vault.capacityUi ? `${fmtUnits(vault.capacityUi)} ${sym}` : 'uncapped'} />
          <Row label="Idle" v={`${fmtUnits(vault.idleUi)} ${sym} (${fmtPct(vault.idleRatio, 1)})`} />
          <Row label="LP supply" v={fmtUnits(vault.lpBalanceUi ?? vault.lpBalance)} />
          <Row label="Mgmt fee" v={fmtBp(vault.managementFeeBps)} />
          <Row label="Exit fee (normal / fast)" v={`${fmtBp(vault.normalWithdrawalCutBp)} / ${fmtBp(vault.fastWithdrawalCutBp)}`} />
          <Row label="Pending withdrawals" v={fmtPct(vault.pendingWithdrawRatio, 2)} />
          <Row label="APY since inception (deposit-implied)" v={lifeApy != null ? fmtPct(lifeApy) : '–'} />
          <Row label="Snapshot" v={vault.snapshotDate ?? '–'} />
        </div>
      </div>

      {/* Positions */}
      {(tokens.length > 0 || obligations.length > 0) && (
        <div className="border-t border-white/[0.06] pt-6">
          <SectionTitle>Positions</SectionTitle>
          <div className="grid grid-cols-1 md:grid-cols-2 gap-8">
            <div className="space-y-1 text-[11px]">
              {tokens.map(t => (
                <div key={t.mint} className="flex justify-between">
                  <div className="flex items-center gap-2">
                    <a href={`https://solscan.io/token/${t.mint}`}
                       target="_blank" rel="noopener noreferrer"
                       className="text-white/65 hover:text-white">
                      {t.symbol ?? shortAddr(t.mint)}
                    </a>
                    {t.kind === 'idle' && (
                      <span className="text-[9px] text-white/30 border border-white/10 rounded px-1 py-px">idle</span>
                    )}
                  </div>
                  <span className="text-white/75 tabular-nums">{fmtUnits(t.amountUi)}</span>
                </div>
              ))}
              {tokens.length === 0 && <div className="text-white/30 text-[11px]">No token holdings.</div>}
            </div>
            <div className="space-y-3">
              {obligations.map(o => (
                <div key={o.obligation} className="text-[11px] space-y-1.5">
                  <div className="flex items-center gap-2">
                    <span className="text-white/55">Kamino loop</span>
                    <a href={`https://solscan.io/account/${o.obligation}`}
                       target="_blank" rel="noopener noreferrer"
                       className="font-mono text-white/35 hover:text-white text-[10px]">
                      {shortAddr(o.obligation)} ↗
                    </a>
                    {o.debtValue > 0 && o.leverage != null ? (
                      <span className="text-[10px] text-white/70 border border-white/15 rounded px-1.5 py-px tabular-nums">
                        {o.leverage.toFixed(2)}x looped
                      </span>
                    ) : (
                      <span className="text-[10px] text-white/45 border border-white/10 rounded px-1.5 py-px">
                        lend only
                      </span>
                    )}
                  </div>
                  <Row label="Collateral" v={`$${fmtUnits(o.collateralValue)}`} />
                  <Row label="Borrowed" v={`$${fmtUnits(o.debtValue)}`} />
                  <Row label="Net" v={`$${fmtUnits(o.netValue)}`} />
                  <Row label="LTV (max / liq)"
                       v={`${fmtPct(o.ltv, 1)} (${fmtPct(o.maxLtv, 0)} / ${fmtPct(o.liqLtv, 0)})`} />
                  {o.collateral && o.collateral.length > 0 && (
                    <div className="pt-1">
                      <div className="text-[10px] text-white/35 mb-0.5">
                        Collateral legs{o.collateral.length > 1 ? ` (${o.collateral.length} loops)` : ''}
                      </div>
                      {o.collateral.map(c => (
                        <div key={c.symbol} className="flex justify-between pl-2">
                          <span className="text-white/50">
                            {c.symbol}
                            {c.ramping && <span className="text-white/30 text-[9px] ml-1.5">ramping</span>}
                          </span>
                          <span className="text-white/70 tabular-nums">
                            {c.ramping ? '—' : `$${fmtUnits(c.valueUi)}`}
                          </span>
                        </div>
                      ))}
                      {o.borrows && o.borrows.length > 0 && (
                        <>
                          <div className="text-[10px] text-white/35 mt-1 mb-0.5">Borrowed</div>
                          {o.borrows.map(b => (
                            <div key={b.symbol} className="flex justify-between pl-2">
                              <span className="text-white/50">{b.symbol}</span>
                              <span className="text-white/70 tabular-nums">${fmtUnits(b.valueUi)}</span>
                            </div>
                          ))}
                        </>
                      )}
                    </div>
                  )}
                </div>
              ))}
              {obligations.length === 0 && (
                <div className="text-[11px] text-white/30">No lending positions.</div>
              )}
            </div>
          </div>
        </div>
      )}

      {/* Withdrawal queue */}
      {shard?.withdrawalQueue && shard.withdrawalQueue.count > 0 && (() => {
        const q = shard.withdrawalQueue!;
        const days = q.dueAt ? daysUntil(q.dueAt) : null;
        return (
          <div className="border-t border-white/[0.06] pt-6">
            <SectionTitle>
              Withdrawal queue{' '}
              <span className="normal-case tracking-normal">(exits waiting for the next payout window)</span>
            </SectionTitle>
            <div className="grid grid-cols-2 sm:grid-cols-4 gap-x-8 gap-y-4">
              <Stat label="Going out"
                    value={q.valueUi != null ? `${fmtUnits(q.valueUi)} ${sym}` : `${fmtUnits(q.lpRemainingUi)} LP`}
                    sub={q.pctOfSupply != null ? `${fmtPct(q.pctOfSupply, 2)} of supply` : undefined} />
              <Stat label="Requests" value={String(q.count)}
                    sub={q.oldestCreatedAt ? `oldest ${fmtTimeAgo(q.oldestCreatedAt)}` : undefined} />
              <Stat label="Next payout"
                    value={q.dueAt ? fmtDate(q.dueAt) : '–'}
                    sub={days == null ? undefined
                      : days > 0 ? `in ${days.toFixed(1)} days`
                      : 'window open — payable now'} />
              <div className="space-y-1 text-[11px]">
                <div className="text-[10px] uppercase tracking-[0.18em] text-white/35">Largest requests</div>
                {q.requests.slice(0, 4).map(r => (
                  <div key={r.owner} className="flex justify-between">
                    <a href={`/wallet/?addr=${r.owner}`}
                       className="font-mono text-white/60 hover:text-white">
                      {shortAddr(r.owner)}
                    </a>
                    <span className="text-white/70 tabular-nums">
                      {r.valueUi != null ? `≈ ${fmtUnits(r.valueUi)} ${sym}` : `${fmtUnits(r.lpUi)} LP`}
                    </span>
                  </div>
                ))}
              </div>
            </div>
          </div>
        );
      })()}

      {/* Vault parameter changes (governance proposals) */}
      {(shard?.proposals?.length ?? 0) > 0 && (
        <div className="border-t border-white/[0.06] pt-6">
          <SectionTitle>
            Vault parameter changes{' '}
            <span className="normal-case tracking-normal">
              (governance proposals — how the manager rebalances and re-permissions the vault)
            </span>
          </SectionTitle>
          <div className="grid grid-cols-12 gap-2 text-[10px] uppercase tracking-[0.14em] text-white/30 pb-1 border-b border-white/[0.08]">
            <span className="col-span-3 md:col-span-2">Proposal</span>
            <span className="col-span-3 md:col-span-2">Status</span>
            <span className="col-span-3 md:col-span-3">Time</span>
            <span className="col-span-2 md:col-span-3 text-right">Reject threshold</span>
            <span className="col-span-1 md:col-span-2 text-right">Changes</span>
          </div>
          <div className="text-[12px]">
            {shard!.proposals!.map(p => (
              <ProposalRow key={p.id} p={p} threshold={shard!.rejectThreshold} />
            ))}
          </div>
        </div>
      )}

      {/* Top LPs */}
      <div className="border-t border-white/[0.06] pt-6">
        <SectionTitle>
          Top LPs <span className="normal-case tracking-normal">(largest depositors by vault-share balance)</span>
        </SectionTitle>
        {vault.topHolders && vault.topHolders.length > 0 ? (
          <div className="grid grid-cols-1 md:grid-cols-2 gap-x-10 gap-y-1 text-[11px]">
            {vault.topHolders.map((h, i) => {
              const shareOfSupply = vault.lpBalanceUi ? h.lpUi / vault.lpBalanceUi : null;
              const value = vault.navPerShare != null ? h.lpUi * vault.navPerShare : null;
              return (
                <div key={h.wallet} className="flex justify-between">
                  <div className="flex items-center gap-2">
                    <span className="text-white/30 w-4 text-right tabular-nums">{i + 1}</span>
                    <a href={`/wallet/?addr=${h.wallet}`}
                       className="font-mono text-white/65 hover:text-white">
                      {shortAddr(h.wallet)}
                    </a>
                    {shareOfSupply != null && (
                      <span className="text-white/35 tabular-nums text-[10px]">{fmtPct(shareOfSupply, 1)}</span>
                    )}
                  </div>
                  <span className="text-white/75 tabular-nums">
                    {value != null ? `≈ ${fmtUnits(value)} ${sym}` : `${fmtUnits(h.lpUi)} LP`}
                  </span>
                </div>
              );
            })}
          </div>
        ) : (
          <div className="text-[11px] text-white/30">No LPs indexed.</div>
        )}
      </div>

      {/* Transactions */}
      <div className="border-t border-white/[0.06] pt-6">
        <div className="flex items-center justify-between flex-wrap gap-2 mb-1">
          <div className="flex gap-4 flex-wrap">
            {([['user', `User transactions (${userTxs.length})`],
               ['activity', `Vault activity (${executions.length})`],
               ['governance', `Governance (${vaultTxs.length})`]] as [TxTab, string][]).map(([key, label]) => (
              <button key={key} onClick={() => setTxTab(key)}
                className={`text-[12px] pb-1 border-b transition ${
                  txTab === key
                    ? 'text-white border-white'
                    : 'text-white/40 border-transparent hover:text-white/70'
                }`}>
                {label}
              </button>
            ))}
          </div>
          {txTab === 'user' ? (
            <div className="flex gap-1.5 text-[11px]">
              {(['all', 'deposits', 'withdrawals'] as TxFilter[]).map(f => (
                <button key={f} onClick={() => setTxFilter(f)}
                  className={`border rounded px-2 py-0.5 capitalize transition ${
                    txFilter === f
                      ? 'border-white/30 text-white'
                      : 'border-white/10 text-white/40 hover:text-white/70'
                  }`}>
                  {f}
                </button>
              ))}
            </div>
          ) : txTab === 'governance' ? (
            <div className="flex gap-1.5 text-[11px] flex-wrap">
              {OP_FILTERS.map(f => (
                <button key={f.key} onClick={() => setOpFilter(f.key)}
                  className={`border rounded px-2 py-0.5 transition ${
                    opFilter === f.key
                      ? 'border-white/30 text-white'
                      : 'border-white/10 text-white/40 hover:text-white/70'
                  }`}>
                  {f.label}
                </button>
              ))}
            </div>
          ) : null}
        </div>
        <p className="text-[10px] text-white/30 mb-3">
          {txTab === 'user'
            ? 'Deposits and withdrawals by depositors.'
            : txTab === 'activity'
              ? 'The manager’s executed strategy legs: trades, lending operations, and swaps performed through whitelisted protocols. Amounts are the vault’s token balance changes.'
              : 'Manager and governance actions: position updates, proposal cycles, policy changes, withdrawal fills. Keeper cranks (AUM refresh, oracle updates) are hidden.'}
        </p>
        {txTab === 'activity' ? (
          executions.length > 0 ? (
            <>
              <div className="grid grid-cols-12 gap-2 text-[10px] uppercase tracking-[0.14em] text-white/30 pb-1 border-b border-white/[0.08]">
                <span className="col-span-3 md:col-span-2">Time</span>
                <span className="col-span-3 md:col-span-2">Type</span>
                <span className="col-span-3 md:col-span-4 text-right">Asset</span>
                <span className="col-span-2 md:col-span-3 text-right">Protocol</span>
                <span className="col-span-1 text-right">Tx</span>
              </div>
              <div className="text-[11px]">
                {executions.slice(0, shown).map((ex, i) => (
                  <div key={`${ex.sig}-${i}`}
                       className="grid grid-cols-12 gap-2 items-center py-1.5 border-b border-white/[0.04]">
                    <span className="col-span-3 md:col-span-2 text-white/45 tabular-nums">
                      {fmtDate(ex.blockTime)}
                      <span className="text-white/25 text-[10px] ml-1.5 hidden md:inline">
                        {fmtTimeAgo(ex.blockTime)}
                      </span>
                    </span>
                    <span className="col-span-3 md:col-span-2 flex gap-1 flex-wrap">
                      {(ex.types.length ? ex.types : ['Interaction']).map(t => (
                        <span key={t}
                              className={`text-[10px] rounded px-1.5 py-0.5 ${executionTypeColor(t)}`}>
                          {t}
                        </span>
                      ))}
                    </span>
                    <span className="col-span-3 md:col-span-4 text-right text-white/70 tabular-nums truncate">
                      {ex.amountUi != null
                        ? <>{fmtUnits(Math.abs(ex.amountUi))} <span className="text-white/40">{ex.symbol}</span></>
                        : ''}
                    </span>
                    <span className="col-span-2 md:col-span-3 text-right text-white/50 truncate">
                      {ex.protocols.join(', ')}
                    </span>
                    <a href={`https://solscan.io/tx/${ex.sig}`}
                       target="_blank" rel="noopener noreferrer"
                       className="col-span-1 text-right text-white/30 hover:text-white text-[10px]">↗</a>
                  </div>
                ))}
              </div>
              {executions.length > shown && (
                <button onClick={() => setShown(shown + PAGE)}
                  className="mt-3 text-[11px] text-white/40 hover:text-white border border-white/10 rounded px-3 py-1 transition">
                  Show {Math.min(PAGE, executions.length - shown)} more ({executions.length - shown} remaining)
                </button>
              )}
            </>
          ) : (
            <div className="text-[11px] text-white/30">
              No execution activity indexed yet — run the executions extractor.
            </div>
          )
        ) : txs.length > 0 ? (
          <>
            {/* Column headers */}
            <div className="grid grid-cols-12 gap-2 text-[10px] uppercase tracking-[0.14em] text-white/30 pb-1 border-b border-white/[0.08]">
              <span className="col-span-2">Date</span>
              <span className="col-span-3">Action</span>
              <span className="col-span-3">Actor</span>
              <span className="col-span-2 text-right">
                {txTab === 'governance' ? 'LP Δ' : 'Amount'}
              </span>
              <span className="col-span-1 text-right">Detail</span>
              <span className="col-span-1 text-right">Tx</span>
            </div>
            <div className="text-[11px]">
              {txs.slice(0, shown).map((ev, i) => {
                const size = activitySize(ev, uDec, lpDec);
                const role = actorRole(ev.actor, roles);
                const detail = txTab === 'governance' ? vaultOpDetail(ev) : null;
                return (
                  <div key={`${ev.sig}-${i}`}
                       className="grid grid-cols-12 gap-2 items-center py-1.5 border-b border-white/[0.04]">
                    <span className="col-span-2 text-white/45 tabular-nums">
                      {fmtDate(ev.blockTime)}
                      <span className="text-white/25 text-[10px] ml-1.5 hidden md:inline">
                        {fmtTimeAgo(ev.blockTime)}
                      </span>
                    </span>
                    <span className={`col-span-3 ${actionColor(ev.ixName)} truncate`}>
                      {ev.ixName}
                    </span>
                    <span className="col-span-3 flex items-center gap-1.5 min-w-0">
                      <a href={`/wallet/?addr=${ev.actor}`}
                         className="font-mono text-white/60 hover:text-white truncate">
                        {shortAddr(ev.actor)}
                      </a>
                      {role && (
                        <span className="text-[9px] text-white/45 border border-white/15 rounded px-1 py-px shrink-0">
                          {role}
                        </span>
                      )}
                    </span>
                    <span className="col-span-2 text-right text-white/60 tabular-nums">
                      {size != null ? fmtUnits(size) : ''}
                    </span>
                    <span className="col-span-1 text-right text-white/40 text-[10px] truncate">
                      {detail ?? ''}
                    </span>
                    <a href={`https://solscan.io/tx/${ev.sig}`}
                       target="_blank" rel="noopener noreferrer"
                       className="col-span-1 text-right text-white/30 hover:text-white text-[10px]">↗</a>
                  </div>
                );
              })}
            </div>
            {txs.length > shown && (
              <button onClick={() => setShown(shown + PAGE)}
                className="mt-3 text-[11px] text-white/40 hover:text-white border border-white/10 rounded px-3 py-1 transition">
                Show {Math.min(PAGE, txs.length - shown)} more ({txs.length - shown} remaining)
              </button>
            )}
          </>
        ) : (
          <div className="text-[11px] text-white/30">No transactions indexed.</div>
        )}
        {vault.flows && (
          <div className="mt-3 text-[10px] text-white/45 flex gap-4 flex-wrap">
            <span>{vault.flows.totalDeposits} deposits</span>
            <span>{vault.flows.totalQueues} queued withdrawals</span>
            <span>{vault.flows.totalExecutes} executed</span>
            <span>{vault.flows.totalFastWithdraws} fast exits</span>
            <span>{vault.flows.uniqueActors} unique wallets</span>
          </div>
        )}
      </div>
    </div>
  );
}

function StrategyPageInner() {
  const params = useSearchParams();
  const addr = params.get('addr');
  const [data, setData] = useState<StrategyVaultData | null>(null);
  const [err, setErr] = useState<string | null>(null);

  useEffect(() => {
    fetch('/strategy_vault.json')
      .then(r => (r.ok ? r.json() : Promise.reject(`HTTP ${r.status}`)))
      .then(setData)
      .catch(e => setErr(String(e)));
  }, []);

  const vault = useMemo(
    () => data?.vaults.find(v => v.address === addr) ?? null,
    [data, addr],
  );

  return (
    <main className="mx-auto max-w-[1100px] px-4 sm:px-6 py-10">
      <div className="mb-6">
        <Link href="/" className="text-[11px] text-white/40 hover:text-white">← All strategies</Link>
      </div>
      {err && <div className="text-sm text-rose-400/80">Failed to load strategy data: {err}</div>}
      {!err && !data && <div className="text-sm text-white/40">Loading…</div>}
      {!err && data && !vault && (
        <div className="text-sm text-white/40">
          {addr ? `No indexed vault at ${addr}` : 'No vault address given — open a strategy from the Strategies tab.'}
        </div>
      )}
      {vault && <StrategyDetail vault={vault} />}
    </main>
  );
}

export default function StrategyPage() {
  return (
    <Suspense fallback={<div className="text-sm text-white/40 p-10">Loading…</div>}>
      <StrategyPageInner />
    </Suspense>
  );
}
