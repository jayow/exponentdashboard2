'use client';
import { useEffect, useMemo, useState } from 'react';
import {
  PieChart, Pie, Cell, BarChart, Bar, XAxis, YAxis, Tooltip, ResponsiveContainer, TooltipProps,
} from 'recharts';
import { colorForPlatform } from '@/lib/colors';

type TvlData = { byPlatform: Record<string, number[]>; dates: string[] };

function fmtUsd(n: number) {
  if (Math.abs(n) >= 1e9) return `$${(n / 1e9).toFixed(2)}B`;
  if (Math.abs(n) >= 1e6) return `$${(n / 1e6).toFixed(2)}M`;
  if (Math.abs(n) >= 1e3) return `$${(n / 1e3).toFixed(1)}K`;
  return `$${n.toFixed(0)}`;
}

// Hide dust platforms — must clear BOTH thresholds:
//   * ≥ $10K absolute  (visible by itself)
//   * ≥ 0.1% of total  (visible relative to leaders)
const MIN_USD_ABS = 10_000;
const MIN_PCT_OF_TOTAL = 0.001;
const CHART_HEIGHT = 280;  // both cards use the same chart height

// Tooltip showing "Platform: $X (Y%)". For the horizontal bar chart we
// can't rely on Recharts' default series-name (it would say "value")
// because dataKey is "value" and nameKey isn't an honored option on Bar.
function PlatformTooltip({ active, payload, total }: TooltipProps<number, string> & { total: number }) {
  if (!active || !payload?.length) return null;
  const p = payload[0];
  const value = typeof p.value === 'number' ? p.value : 0;
  const platform = (p.payload as any)?.platform ?? p.name ?? '—';
  return (
    <div className="bg-[#0a0a0a] border border-white/15 rounded-lg p-2 text-xs">
      <div className="flex items-center gap-2">
        <span className="w-2 h-2 rounded-sm" style={{ backgroundColor: (p as any).color || (p as any).fill }} />
        <span className="text-white/85">{platform}</span>
      </div>
      <div className="text-white tabular-nums mt-1">
        {fmtUsd(value)}{' '}
        <span className="text-white/40">({((value / (total || 1)) * 100).toFixed(1)}%)</span>
      </div>
    </div>
  );
}

export function TvlByPlatform() {
  const [data, setData] = useState<TvlData | null>(null);
  useEffect(() => { fetch('/tvl.json').then(r => r.json()).then(setData).catch(() => null); }, []);

  const rows = useMemo(() => {
    if (!data) return [];
    const all = Object.entries(data.byPlatform)
      .map(([platform, series]) => ({ platform, value: series[series.length - 1] || 0 }))
      .sort((a, b) => b.value - a.value);
    const total = all.reduce((s, r) => s + r.value, 0) || 1;
    return all.filter(r => r.value >= MIN_USD_ABS && r.value / total >= MIN_PCT_OF_TOTAL);
  }, [data]);

  const total = rows.reduce((s, r) => s + r.value, 0);

  if (!data) return <div className="text-white/40 text-sm p-4">Loading platforms…</div>;
  if (!rows.length) return null;

  return (
    <div className="grid grid-cols-1 lg:grid-cols-2 gap-4 mb-6">
      {/* Horizontal bar chart */}
      <section className="rounded-2xl border border-white/10 bg-white/5 p-4 flex flex-col">
        <h3 className="text-[11px] uppercase tracking-wider text-white/40 mb-3">TVL by platform</h3>
        <div className="flex-1">
          <ResponsiveContainer width="100%" height={CHART_HEIGHT}>
            <BarChart data={rows} layout="vertical" margin={{ top: 8, right: 60, left: 0, bottom: 8 }}>
              <XAxis type="number" tick={{ fill: '#888', fontSize: 11 }} tickFormatter={fmtUsd} axisLine={false} tickLine={false} />
              <YAxis type="category" dataKey="platform" tick={{ fill: '#aaa', fontSize: 11 }} width={90} axisLine={false} tickLine={false} />
              <Tooltip cursor={{ fill: 'rgba(255,255,255,0.04)' }} content={<PlatformTooltip total={total} />} />
              <Bar dataKey="value" radius={[0, 4, 4, 0]} barSize={24}
                   label={{ position: 'right', fill: '#bbb', fontSize: 11, formatter: (v: any) => fmtUsd(Number(v)) }}>
                {rows.map(r => <Cell key={r.platform} fill={colorForPlatform(r.platform)} />)}
              </Bar>
            </BarChart>
          </ResponsiveContainer>
        </div>
      </section>

      {/* Donut chart — no stroke between slices */}
      <section className="rounded-2xl border border-white/10 bg-white/5 p-4 flex flex-col">
        <h3 className="text-[11px] uppercase tracking-wider text-white/40 mb-3">TVL share</h3>
        <div className="flex-1 relative">
          <ResponsiveContainer width="100%" height={CHART_HEIGHT}>
            <PieChart margin={{ top: 0, right: 0, bottom: 0, left: 0 }}>
              <Pie data={rows} dataKey="value" nameKey="platform"
                   cx="50%" cy="50%" innerRadius={70} outerRadius={110}
                   paddingAngle={0} stroke="none"
                   label={({ percent }: any) => percent >= 0.04 ? `${(percent * 100).toFixed(0)}%` : ''}
                   labelLine={false}>
                {rows.map(r => <Cell key={r.platform} fill={colorForPlatform(r.platform)} />)}
              </Pie>
              <Tooltip content={<PlatformTooltip total={total} />} />
            </PieChart>
          </ResponsiveContainer>
          {/* Center label */}
          <div className="absolute inset-0 flex flex-col items-center justify-center pointer-events-none">
            <div className="text-[10px] uppercase tracking-wider text-white/40">Total</div>
            <div className="text-base font-semibold tabular-nums text-white/90">{fmtUsd(total)}</div>
          </div>
        </div>
        {/* Legend */}
        <div className="flex flex-wrap gap-x-3 gap-y-1 mt-2 text-[11px] justify-center">
          {rows.map(r => (
            <span key={r.platform} className="flex items-center gap-1.5 text-white/70">
              <span className="w-2 h-2 rounded-sm" style={{ backgroundColor: colorForPlatform(r.platform) }} />
              {r.platform} <span className="text-white/40">{((r.value / total) * 100).toFixed(1)}%</span>
            </span>
          ))}
        </div>
      </section>
    </div>
  );
}
