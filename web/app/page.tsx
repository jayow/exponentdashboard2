'use client';
import { useEffect, useLayoutEffect, useRef, useState } from 'react';
import { TopStats } from '@/components/TopStats';
import { TvlByPlatform } from '@/components/TvlByPlatform';
import { BigChart } from '@/components/BigChart';
import { MarketsList } from '@/components/MarketsList';
import { UsersAnalytics } from '@/components/UsersAnalytics';
import { TranchingAnalytics } from '@/components/TranchingAnalytics';
import { StrategyVaultAnalytics } from '@/components/StrategyVaultAnalytics';

// useLayoutEffect on the client (restore scroll before paint), useEffect on the
// server to avoid the SSR "does nothing on the server" warning.
const useIsoLayoutEffect = typeof document !== 'undefined' ? useLayoutEffect : useEffect;

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
  const [tab, setTabState] = useState<Tab>('markets');
  const [updatedAt, setUpdatedAt] = useState<string | null>(null);
  const mainRef = useRef<HTMLElement>(null);

  // Persist the tab in sessionStorage (NOT the URL — writing the URL makes Next's
  // router re-render the whole page, which flashes on every tab click).
  function setTab(t: Tab) {
    setTabState(t);
    try { sessionStorage.setItem('home:tab', t); } catch { /* noop */ }
  }

  useEffect(() => {
    try {
      const t = sessionStorage.getItem('home:tab') as Tab | null;
      if (t && TABS.some((x) => x.key === t)) setTabState(t);
    } catch { /* noop */ }

    fetch('/stats.json')
      .then((r) => r.json())
      .then((d) => setUpdatedAt(d?.meta?.generatedAt ?? null))
      .catch(() => {});
  }, []);

  // Persist scroll + page height so a back-nav can reserve the space and land there.
  useEffect(() => {
    const save = () => {
      try {
        sessionStorage.setItem('home:scroll', String(window.scrollY));
        sessionStorage.setItem('home:height', String(document.documentElement.scrollHeight));
      } catch { /* noop */ }
    };
    window.addEventListener('scroll', save, { passive: true });
    return () => window.removeEventListener('scroll', save);
  }, []);

  // On back navigation, reserve the prior page height and restore scroll BEFORE
  // paint — so the page appears already at the saved offset instead of flashing
  // at the top and snapping down as async content loads.
  useIsoLayoutEffect(() => {
    let y = 0, h = 0;
    try {
      y = Number(sessionStorage.getItem('home:scroll') || 0);
      h = Number(sessionStorage.getItem('home:height') || 0);
    } catch { /* noop */ }
    if (y < 40) return;
    const main = mainRef.current;
    if (main && h) main.style.minHeight = `${h}px`;
    window.scrollTo(0, y);
    let n = 0;
    const pin = setInterval(() => { window.scrollTo(0, y); if (++n > 16) clearInterval(pin); }, 50);
    const release = setTimeout(() => { if (main) main.style.minHeight = ''; }, 1000);
    return () => { clearInterval(pin); clearTimeout(release); };
  }, []);

  return (
    <main ref={mainRef} className="mx-auto max-w-[1500px] px-4 sm:px-6 py-10">
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
