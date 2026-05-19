'use client';
import { useEffect, useMemo, useState } from 'react';
import { PieChart, Pie, Cell, BarChart, Bar, XAxis, YAxis, Tooltip, ResponsiveContainer } from 'recharts';

type TvlData = { byPlatform: Record<string, number[]>; dates: string[] };

const COLORS = ['#6b66ff', '#ffb74d', '#4ade80', '#f87171', '#38bdf8',
                '#a78bfa', '#fb923c', '#34d399', '#f472b6', '#facc15',
                '#818cf8', '#fbbf24', '#22d3ee', '#e879f9', '#a3e635'];

function fmtUsd(n: number) {
  if (Math.abs(n) >= 1e9) return `$${(n / 1e9).toFixed(2)}B`;
  if (Math.abs(n) >= 1e6) return `$${(n / 1e6).toFixed(2)}M`;
  if (Math.abs(n) >= 1e3) return `$${(n / 1e3).toFixed(1)}K`;
  return `$${n.toFixed(0)}`;
}

export function TvlByPlatform() {
  const [data, setData] = useState<TvlData | null>(null);
  useEffect(() => { fetch('/tvl.json').then(r => r.json()).then(setData).catch(() => null); }, []);

  const rows = useMemo(() => {
    if (!data) return [];
    return Object.entries(data.byPlatform)
      .map(([platform, series]) => ({ platform, value: series[series.length - 1] || 0 }))
      .filter(r => r.value > 0)
      .sort((a, b) => b.value - a.value);
  }, [data]);

  const total = rows.reduce((s, r) => s + r.value, 0);
  // Group the tail into "Other" when ≥6 platforms, to keep the donut readable
  const donutRows = useMemo(() => {
    if (rows.length <= 5) return rows;
    const top = rows.slice(0, 5);
    const otherVal = rows.slice(5).reduce((s, r) => s + r.value, 0);
    return otherVal > 0 ? [...top, { platform: 'Other', value: otherVal }] : top;
  }, [rows]);

  if (!data) return <div className="text-white/40 text-sm p-4">Loading platforms…</div>;
  if (!rows.length) return null;

  return (
    <div className="grid grid-cols-1 lg:grid-cols-2 gap-4 mb-6">
      {/* Horizontal bar chart */}
      <section className="rounded-2xl border border-white/10 bg-white/5 p-4">
        <h3 className="text-[11px] uppercase tracking-wider text-white/40 mb-3">TVL by platform</h3>
        <ResponsiveContainer width="100%" height={Math.max(180, rows.length * 28 + 40)}>
          <BarChart data={rows} layout="vertical" margin={{ top: 0, right: 30, left: 10, bottom: 8 }}>
            <XAxis type="number" tick={{ fill: '#888', fontSize: 11 }} tickFormatter={fmtUsd} axisLine={false} tickLine={false} />
            <YAxis type="category" dataKey="platform" tick={{ fill: '#aaa', fontSize: 11 }} width={90} axisLine={false} tickLine={false} />
            <Tooltip contentStyle={{ background: '#0a0a0a', border: '1px solid rgba(255,255,255,0.15)', fontSize: 11 }}
                     formatter={(v: any) => fmtUsd(Number(v))} />
            <Bar dataKey="value" radius={[0, 4, 4, 0]}>
              {rows.map((_, i) => <Cell key={i} fill={COLORS[i % COLORS.length]} />)}
            </Bar>
          </BarChart>
        </ResponsiveContainer>
      </section>

      {/* Donut chart */}
      <section className="rounded-2xl border border-white/10 bg-white/5 p-4">
        <h3 className="text-[11px] uppercase tracking-wider text-white/40 mb-3">TVL share</h3>
        <ResponsiveContainer width="100%" height={260}>
          <PieChart>
            <Pie data={donutRows} dataKey="value" nameKey="platform"
                 cx="50%" cy="50%" innerRadius={62} outerRadius={100} paddingAngle={2}
                 label={({ platform, percent }: any) => `${(percent * 100).toFixed(1)}%`}
                 labelLine={false}>
              {donutRows.map((_, i) => <Cell key={i} fill={COLORS[i % COLORS.length]} />)}
            </Pie>
            <Tooltip contentStyle={{ background: '#0a0a0a', border: '1px solid rgba(255,255,255,0.15)', fontSize: 11 }}
                     formatter={(v: any, n: any) => [`${fmtUsd(Number(v))} (${((Number(v)/total)*100).toFixed(1)}%)`, n]} />
          </PieChart>
        </ResponsiveContainer>
        <div className="flex flex-wrap gap-2 mt-2 text-[11px] justify-center">
          {donutRows.map((r, i) => (
            <span key={r.platform} className="flex items-center gap-1.5 text-white/70">
              <span className="w-2 h-2 rounded-sm" style={{ backgroundColor: COLORS[i % COLORS.length] }} />
              {r.platform}
            </span>
          ))}
        </div>
      </section>
    </div>
  );
}
