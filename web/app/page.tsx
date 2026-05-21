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
        <div className="flex items-center gap-1 mb-4">
          {TABS.map(t => (
            <button key={t.key} onClick={() => setTab(t.key)}
              className={`text-xs px-3 py-1.5 rounded-lg border transition ${
                tab === t.key ? 'border-white/30 bg-white/10 text-white' : 'border-white/10 text-white/40 hover:text-white'
              }`}>
              {t.label}
            </button>
          ))}
        </div>

        {tab === 'markets' && <MarketsList />}
        {tab === 'users'   && <UsersAnalytics />}
      </div>
    </main>
  );
}
