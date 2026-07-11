'use client';
import { useEffect, useState } from 'react';
import { TopStats } from '@/components/TopStats';
import { TvlByPlatform } from '@/components/TvlByPlatform';
import { BigChart } from '@/components/BigChart';
import { MarketsList } from '@/components/MarketsList';
import { UsersAnalytics } from '@/components/UsersAnalytics';
import { TranchingAnalytics } from '@/components/TranchingAnalytics';
import { StrategyVaultAnalytics } from '@/components/StrategyVaultAnalytics';

type Tab = 'markets' | 'users' | 'tranching' | 'strategies';

const TABS: { key: Tab; label: string }[] = [
  { key: 'markets',    label: 'Markets' },
  { key: 'users',      label: 'Users' },
  { key: 'tranching',  label: 'Tranching' },
  { key: 'strategies', label: 'Strategies' },
];

function fmtUpdated(iso: string): string {
  const d = new Date(iso);
  if (isNaN(d.getTime())) return '';
  return d.toLocaleString('en-US', {
    month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit',
    timeZone: 'UTC', hour12: false,
  }) + ' UTC';
}

export default function HomePage() {
  const [tab, setTab] = useState<Tab>('markets');
  const [updatedAt, setUpdatedAt] = useState<string | null>(null);

  useEffect(() => {
    fetch('/stats.json')
      .then((r) => r.json())
      .then((d) => setUpdatedAt(d?.meta?.generatedAt ?? null))
      .catch(() => {});
  }, []);

  return (
    <main className="mx-auto max-w-[1500px] px-4 sm:px-6 py-10">
      <header className="relative mb-8">
        <div className="flex items-center justify-between gap-3">
          <div className="flex items-center gap-3">
            <a href="https://app.exponent.finance" target="_blank" rel="noopener noreferrer" className="shrink-0">
              <img src="/logos/v2-logo.svg" alt="Exponent" className="h-7" />
            </a>
            <span className="text-[11px] text-white/30 border-l border-white/10 pl-3">
              by <a href="https://hanyon.app" target="_blank" rel="noopener noreferrer" className="text-white/50 hover:text-white transition">Hanyon Analytics</a>
            </span>
          </div>
          {updatedAt && (
            <span className="text-[11px] text-white/30 shrink-0" title={updatedAt}>
              Updated {fmtUpdated(updatedAt)}
            </span>
          )}
        </div>
      </header>

      {/* Always-visible overview */}
      <TopStats />
      <TvlByPlatform />
      <BigChart />

      {/* Tabbed analytics */}
      <div className="mb-8">
        <div className="flex items-center gap-5 mb-6 mt-8 pt-5 border-t border-white/[0.06]">
          {TABS.map(t => {
            const isActive = tab === t.key;
            return (
              <button key={t.key} onClick={() => setTab(t.key)}
                className={`relative pb-2 text-xs transition ${
                  isActive ? 'text-white' : 'text-white/35 hover:text-white/70'
                }`}>
                {t.label}
                {isActive && <span className="absolute left-0 right-0 -bottom-px h-px bg-white" />}
              </button>
            );
          })}
        </div>

        {tab === 'markets'    && <MarketsList />}
        {tab === 'users'      && <UsersAnalytics />}
        {tab === 'tranching'  && <TranchingAnalytics />}
        {tab === 'strategies' && <StrategyVaultAnalytics />}
      </div>
    </main>
  );
}
