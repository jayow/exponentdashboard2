'use client';
import { useState } from 'react';
import { TopStats } from '@/components/TopStats';
import { TvlByPlatform } from '@/components/TvlByPlatform';
import { BigChart } from '@/components/BigChart';
import { MarketsList } from '@/components/MarketsList';
import { UsersAnalytics } from '@/components/UsersAnalytics';

type Tab = 'markets' | 'users';

const TABS: { key: Tab; label: string }[] = [
  { key: 'markets', label: 'Markets' },
  // Users tab now includes leaderboard, cohorts, growth, retention,
  // whale activity, and per-market holder concentration as sub-views.
  // "Holders" used to be a separate tab — merged in to match v1 where
  // holders and users are the same population.
  { key: 'users',   label: 'Users' },
];

export default function HomePage() {
  const [tab, setTab] = useState<Tab>('markets');

  return (
    <main className="mx-auto max-w-[1500px] px-4 sm:px-6 py-10">
      <header className="relative mb-8">
        <div className="flex items-center gap-3">
          <a href="https://app.exponent.finance" target="_blank" rel="noopener noreferrer" className="shrink-0">
            <img src="/logos/v2-logo.svg" alt="Exponent" className="h-7" />
          </a>
          <span className="text-[11px] text-white/30 border-l border-white/10 pl-3">
            by <a href="https://hanyon.app" target="_blank" rel="noopener noreferrer" className="text-white/50 hover:text-white transition">Hanyon Analytics</a>
          </span>
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

        {tab === 'markets' && <MarketsList />}
        {tab === 'users'   && <UsersAnalytics />}
      </div>
    </main>
  );
}
