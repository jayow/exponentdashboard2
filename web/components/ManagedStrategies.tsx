'use client';
import { useEffect, useMemo, useState } from 'react';
import Link from 'next/link';
import {
  StrategyVaultData, Vault,
  fmtPct, fmtUnits, shortAddr,
} from '@/lib/strategy-vault';

function CapacityBar({ used, cap }: { used: number; cap: number | null | undefined }) {
  if (!cap || cap <= 0) return null;
  const pct = Math.min(1, used / cap);
  return (
    <div className="h-1 w-full bg-white/[0.07] rounded overflow-hidden mt-2">
      <div className="h-full bg-white/45 rounded" style={{ width: `${(pct * 100).toFixed(1)}%` }} />
    </div>
  );
}

function StrategyRow({ vault }: { vault: Vault }) {
  const sym = vault.underlyingSymbol ?? '?';
  const loop = (vault.positions?.obligations ?? []).find(o => o.debtValue > 0 && o.leverage != null);

  return (
    <Link href={`/strategy/?addr=${vault.address}`}
          className="block border border-white/[0.08] rounded-lg p-5 hover:border-white/25 transition group">
      <div className="grid grid-cols-2 md:grid-cols-12 gap-x-6 gap-y-3 items-center">
        {/* Identity */}
        <div className="col-span-2 md:col-span-4 min-w-0">
          <div className="flex items-center gap-2 flex-wrap">
            <span className="text-white text-[14px]">{vault.name ?? shortAddr(vault.address)}</span>
            {vault.profile && (
              <span className="text-[10px] text-white/40 border border-white/10 rounded px-1.5 py-px capitalize">
                {vault.profile}
              </span>
            )}
            {loop && (
              <span className="text-[10px] text-white/70 border border-white/15 rounded px-1.5 py-px tabular-nums">
                {loop.leverage!.toFixed(2)}x looped
              </span>
            )}
          </div>
          <div className="text-[11px] text-white/40 mt-0.5">
            {vault.strategist ? `by ${vault.strategist}` : shortAddr(vault.address)}
            {vault.depositTicker ? ` · deposits in ${vault.depositTicker}` : ''}
          </div>
        </div>

        {/* AUM / capacity */}
        <div className="md:col-span-3">
          <div className="text-[10px] uppercase tracking-[0.18em] text-white/35">AUM / cap</div>
          <div className="text-white tabular-nums text-[13px] mt-0.5">
            {fmtUnits(vault.aumUi)} / {vault.capacityUi ? fmtUnits(vault.capacityUi) : '∞'} {sym}
            {vault.pctFull != null && vault.pctFull > 0 && (
              <span className="text-white/40 text-[11px]"> · {fmtPct(vault.pctFull, 0)} full</span>
            )}
          </div>
          <CapacityBar used={vault.aumUi ?? 0} cap={vault.capacityUi} />
        </div>

        {/* APY */}
        <div className="md:col-span-2 md:text-right">
          <div className="text-[10px] uppercase tracking-[0.18em] text-white/35">APY 7d / 30d</div>
          <div className="text-white tabular-nums text-[13px] mt-0.5">
            {fmtPct(vault.exponentApy?.d7, 1)} / {fmtPct(vault.exponentApy?.d30, 1)}
          </div>
          <div className="text-[10px] text-white/35">realized</div>
        </div>

        {/* Depositors */}
        <div className="md:col-span-2 md:text-right">
          <div className="text-[10px] uppercase tracking-[0.18em] text-white/35">Depositors</div>
          <div className="text-white tabular-nums text-[13px] mt-0.5">{vault.flows?.uniqueActors ?? 0}</div>
          <div className="text-[10px] text-white/35">{fmtPct(vault.deployedRatio, 0)} deployed</div>
        </div>

        {/* Drill-in */}
        <div className="md:col-span-1 md:text-right text-[12px] text-white/35 group-hover:text-white transition">
          →
        </div>
      </div>
    </Link>
  );
}

export function ManagedStrategies() {
  const [data, setData] = useState<StrategyVaultData | null>(null);
  const [err, setErr] = useState<string | null>(null);

  useEffect(() => {
    fetch('/strategy_vault.json')
      .then(r => (r.ok ? r.json() : Promise.reject(`HTTP ${r.status}`)))
      .then(setData)
      .catch(e => setErr(String(e)));
  }, []);

  const managed = useMemo(() => {
    if (!data?.vaults) return [];
    // Curated order from the pipeline (aumUi mixes denominations — SOL vs USD)
    return data.vaults
      .filter(v => v.isManaged)
      .sort((a, b) => (a.managedOrder ?? 99) - (b.managedOrder ?? 99));
  }, [data]);

  if (err) return <div className="text-sm text-rose-400/80">Failed to load strategy data: {err}</div>;
  if (!data) return <div className="text-sm text-white/40">Loading…</div>;

  if (!managed.length) {
    return (
      <div className="border-t border-b border-white/[0.06] py-12 text-center">
        <p className="text-sm text-white/40">
          No managed strategies in the current snapshot — run the strategy-vault extract + serve refresh.
        </p>
      </div>
    );
  }

  return (
    <div className="space-y-4">
      <p className="text-[11px] text-white/35 -mt-1">
        Managed strategy vaults listed on Exponent. APYs are realized (trailing NAV growth);
        Exponent&apos;s app headline is a forward projection, so the two differ by design.
        Click a strategy for positions, transactions, and full analytics.
      </p>
      {managed.map(v => <StrategyRow key={v.address} vault={v} />)}
      <div className="text-[11px] text-white/25 pt-2">
        Snapshot {managed[0]?.snapshotDate ?? '–'} · Source: {data.meta.source}
      </div>
    </div>
  );
}
