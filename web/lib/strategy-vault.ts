// Shared types + helpers for strategy-vault views (overview tab, all-vaults
// list, and the /strategy/ detail page). Data shape mirrors
// serve/build_web_data.py build_strategy_vault_json.

export type FlowsSummary = {
  totalDeposits: number;
  totalQueues: number;
  totalExecutes: number;
  totalFastWithdraws: number;
  uniqueActors: number;
};

export type ActivityEvent = {
  sig: string;
  blockTime: number;
  actor: string;
  ixName: string;
  lpDelta: number | null;
  baseDelta: number | null;
  argsJson: string | null;
};

export type TopHolder = { wallet: string; lpAmount: number; lpUi: number };

export type VaultHistory = {
  dates: string[];
  aumUi: (number | null)[];
  lpBalance: (number | null)[];
  navPerShare: (number | null)[];
  apy7d: (number | null)[];
  apy30d: (number | null)[];
  netFlowLp: (number | null)[];
  uniqueActors: (number | null)[];
};

export type ImpliedNav = { dates: string[]; nav: number[] };

export type PositionToken = {
  mint: string;
  symbol: string | null;
  amountUi: number;
  kind: 'idle' | 'position';
};

export type PositionObligation = {
  obligation: string;
  collateralValue: number;
  debtValue: number;
  netValue: number;
  ltv: number | null;
  maxLtv: number | null;
  liqLtv: number | null;
  leverage: number | null;
};

export type Positions = { tokens: PositionToken[]; obligations: PositionObligation[] };

export type ExponentApy = {
  d3: number | null;
  d7: number | null;
  d30: number | null;
  current: number | null;
  ptWeight: number | null;
};

export type Vault = {
  address: string;
  name?: string | null;
  isManaged?: boolean;
  managedOrder?: number | null;
  strategist?: string | null;
  profile?: string | null;
  description?: string | null;
  depositTicker?: string | null;
  capacityUi?: number | null;
  exponentApy?: ExponentApy | null;
  underlyingMint: string;
  underlyingSymbol: string | null;
  underlyingDecimals: number | null;
  mintLp: string;
  squadsVault: string | null;
  aumInBase: number | null;
  aumUi: number | null;
  idleUi?: number | null;
  deployedUi?: number | null;
  lpBalance: number | null;
  lpBalanceUi?: number | null;
  lpDecimals?: number | null;
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
  impliedNav?: ImpliedNav | null;
  positions?: Positions | null;
  topHolders?: TopHolder[];
  recentActivity?: ActivityEvent[];
  managerActivity?: ActivityEvent[];
  flows?: FlowsSummary;
};

export type StrategyVaultData = {
  meta: { generatedAt: string; source: string; count: number; snapshotDate?: string | null };
  vaults: Vault[];
};

export function shortAddr(a: string) {
  return `${a.slice(0, 4)}…${a.slice(-4)}`;
}

export function fmtUnits(n: number | null | undefined, decimals = 2) {
  if (n == null) return '–';
  if (Math.abs(n) >= 1e9) return `${(n / 1e9).toFixed(2)}B`;
  if (Math.abs(n) >= 1e6) return `${(n / 1e6).toFixed(2)}M`;
  if (Math.abs(n) >= 1e3) return `${(n / 1e3).toFixed(1)}K`;
  return n.toFixed(decimals);
}

export function fmtPct(n: number | null | undefined, digits = 2) {
  if (n == null) return '–';
  return `${(n * 100).toFixed(digits)}%`;
}

export function fmtBp(n: number | null | undefined) {
  if (n == null) return '–';
  return `${(n / 100).toFixed(2)}%`;
}

export function fmtTimeAgo(ts: number): string {
  const sec = Math.max(0, Math.floor(Date.now() / 1000) - ts);
  if (sec < 60) return `${sec}s ago`;
  if (sec < 3600) return `${Math.floor(sec / 60)}m ago`;
  if (sec < 86400) return `${Math.floor(sec / 3600)}h ago`;
  return `${Math.floor(sec / 86400)}d ago`;
}

export function actionColor(ixName: string): string {
  if (ixName === 'deposit_liquidity') return 'text-emerald-400/80';
  if (ixName.startsWith('execute_') || ixName === 'queue_withdrawal') return 'text-rose-400/80';
  if (ixName === 'fill_withdrawal') return 'text-amber-400/70';
  return 'text-white/45';
}

export function parseArgs(argsJson: string | null): Record<string, number> | null {
  if (!argsJson) return null;
  try { return JSON.parse(argsJson); } catch { return null; }
}

// Activity arg sizes: deposits are base atoms, queue/fast-withdraw LP atoms.
// Falls back to the observed LP balance change (e.g. fill_withdrawal, whose
// args are an opaque vec).
export function activitySize(ev: ActivityEvent, uDec: number, lpDec: number): number | null {
  const args = parseArgs(ev.argsJson);
  if (args?.depositTokenAmountIn != null) return args.depositTokenAmountIn / 10 ** uDec;
  if (args?.queueLpAmount != null) return args.queueLpAmount / 10 ** lpDec;
  if (args?.fastWithdrawLpAmount != null) return args.fastWithdrawLpAmount / 10 ** lpDec;
  if (ev.lpDelta) return Math.abs(ev.lpDelta) / 10 ** lpDec;
  return null;
}

// Annualized growth between first and last point of a dated NAV series.
// Needs >= 2 points spanning >= 3 days; returns null otherwise.
export function inceptionApy(dates: string[], nav: number[]): number | null {
  if (dates.length < 2) return null;
  const first = nav[0];
  const last = nav[nav.length - 1];
  if (!(first > 0) || !(last > 0)) return null;
  const days = (Date.parse(dates[dates.length - 1]) - Date.parse(dates[0])) / 86400000;
  if (days < 3) return null;
  return Math.pow(last / first, 365 / days) - 1;
}

export function lastNonNull(values: (number | null)[] | undefined): number | null {
  if (!values) return null;
  for (let i = values.length - 1; i >= 0; i--) {
    const v = values[i];
    if (v != null && isFinite(v)) return v;
  }
  return null;
}

// Per-vault full-history shard: web/public/strategy-txns/<addr>.json
export type ActorRoles = {
  admin?: string | null;
  sentinel?: string | null;
  strategist?: string | null;
  depositors?: string | null;
};

export type ProposalActionSummary = {
  name: string | null;
  mode: string | null;
  assets: string[];
  protocols: string[];
  verbs: string[];
};

export type Proposal = {
  id: number;
  proposer: string;
  status: 'Active' | 'Approved' | 'Rejected' | 'Executed' | 'Cancelled' | 'Draft' | string;
  createdAt: number;
  votingEndsAt: number;
  executableAt: number;
  rejectVotes: number;
  optOutVotes: number;
  lpSnapshot: number;
  actions: ProposalActionSummary[];
  parseError: boolean;
};

export type ExecutionRow = {
  sig: string;
  blockTime: number;
  types: string[];
  protocols: string[];
  symbol: string | null;
  amountUi: number | null;
  deltas: { symbol: string; amountUi: number }[];
};

export type WithdrawalRequest = {
  owner: string;
  lpUi: number;
  valueUi: number | null;
  createdAt: number;
};

export type WithdrawalQueue = {
  count: number;
  lpRequestedUi: number;
  lpRemainingUi: number;
  valueUi: number | null;
  pctOfSupply: number | null;
  oldestCreatedAt: number | null;
  dueAt: number | null;
  requests: WithdrawalRequest[];
};

export type StrategyTxnShard = {
  address: string;
  generatedAt: string;
  roles: ActorRoles;
  rejectThreshold?: number | null;
  withdrawalQueue?: WithdrawalQueue | null;
  proposals?: Proposal[];
  userActivity: ActivityEvent[];
  vaultActivity: ActivityEvent[];
  executions?: ExecutionRow[];
};

// Days from now until ts (fractional); negative = past due.
export function daysUntil(ts: number): number {
  return (ts * 1000 - Date.now()) / 86400000;
}

// Filled chips (no border) — border outlines wash out at this size.
export function executionTypeColor(t: string): string {
  if (t === 'Deposit' || t === 'Mint SY' || t === 'Repay' || t === 'Deposit SY') {
    return 'text-emerald-400 bg-emerald-400/10';
  }
  if (t === 'Borrow' || t === 'Withdraw' || t === 'Redeem SY' || t === 'Withdraw SY') {
    return 'text-rose-400 bg-rose-400/10';
  }
  return 'text-white/70 bg-white/[0.08]';
}

export function proposalStatusColor(status: string): string {
  if (status === 'Executed') return 'text-emerald-400 bg-emerald-400/10';
  if (status === 'Active' || status === 'Approved') return 'text-sky-400 bg-sky-400/10';
  if (status === 'Rejected') return 'text-rose-400 bg-rose-400/10';
  return 'text-white/60 bg-white/[0.08]';
}

export function actorRole(addr: string, roles: ActorRoles | null | undefined): string | null {
  if (!roles) return null;
  if (addr === roles.admin) return 'admin';
  if (addr === roles.sentinel) return 'sentinel';
  if (addr === roles.strategist) return 'strategist';
  if (addr === roles.depositors) return 'depositors';
  return null;
}

export type VaultOpGroup = 'proposals' | 'fills' | 'policies' | 'settings' | 'position';

const OP_GROUPS: Record<VaultOpGroup, string[]> = {
  proposals: ['init_proposal', 'append_proposal_actions', 'activate_proposal',
              'finalize_proposal', 'execute_proposal', 'cancel_proposal',
              'stake_vote', 'unstake_vote'],
  fills:     ['fill_withdrawal'],
  policies:  ['add_policy', 'update_policy', 'remove_policy',
              'wrapper_add_policy', 'wrapper_update_policy', 'wrapper_remove_policy',
              'update_policy_manager'],
  position:  ['manager_update_position'],
  settings:  [],  // fallback bucket — everything else
};

export function vaultOpGroup(ixName: string): VaultOpGroup {
  for (const [group, names] of Object.entries(OP_GROUPS) as [VaultOpGroup, string[]][]) {
    if (names.includes(ixName)) return group;
  }
  return 'settings';
}

// Short detail string for a vault op, from its decoded args where useful.
export function vaultOpDetail(ev: ActivityEvent): string | null {
  const args = parseArgs(ev.argsJson);
  if (args?.proposal_id != null) return `proposal #${args.proposal_id}`;
  return null;
}

export function fmtDate(ts: number): string {
  return new Date(ts * 1000).toISOString().slice(0, 10);
}
