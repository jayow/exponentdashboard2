// Shared color system across all charts.
//
// Design principle: pick ONE base hue per platform; everything inside that
// platform (its tickers, its individual markets) uses shades of that hue.
// Cross-chart consistency comes for free because every component asks the
// same helper for a color given (platform | ticker | marketKey).

// Platform → base color (mid-saturation, mid-lightness so darker/lighter
// shades stay readable on dark backgrounds).
export const PLATFORM_BASE: Record<string, string> = {
  Solstice:   '#a78bfa',  // violet
  OnRe:       '#fb923c',  // orange
  BULK:       '#4ade80',  // green
  Hylo:       '#f87171',  // rose
  Fragmetric: '#38bdf8',  // sky
  Jito:       '#22d3ee',  // cyan
  Jupiter:    '#34d399',  // emerald
  Drift:      '#818cf8',  // indigo
  Sanctum:    '#f472b6',  // pink
  Kyros:      '#facc15',  // yellow
  Kuru:       '#a3e635',  // lime
  Carrot:     '#fbbf24',  // amber
  Ore:        '#e879f9',  // fuchsia
  Ethena:     '#fb7185',  // rose-400
  Perena:     '#60a5fa',  // blue-400
  Maple:      '#fcd34d',  // amber-300 — syrupUSDC / syUSDC
  MarginFi:   '#c084fc',  // purple-400
  Asgard:     '#86efac',  // green-300 (no active markets currently)
  Adrena:     '#f59e0b',  // amber-500 — ALP
  Reflect:    '#2dd4bf',  // teal-400 — USDC+
  Marinade:   '#fda4af',  // pink-300 — mUSDC
  Other:      '#9ca3af',  // gray-400
};

const FALLBACK = '#9ca3af';

// Ticker → platform mapping (mirrors normalize_platform's ticker fallback
// in serve/build_web_data.py). Lets charts that key off ticker map back
// to the right hue family.
const TICKER_TO_PLATFORM: Record<string, string> = {
  USX: 'Solstice', eUSX: 'Solstice',
  ONyc: 'OnRe',
  BulkSOL: 'BULK', wBulkSOL: 'BULK',
  hyloSOL: 'Hylo', 'hyloSOL+': 'Hylo', 'hySOL+': 'Hylo', hyUSD: 'Hylo',
  sHYUSD: 'Hylo', xSOL: 'Hylo',
  fragSOL: 'Fragmetric', fragBTC: 'Fragmetric', wfragBTC: 'Fragmetric',
  JitoSOL: 'Jito',
  JLP: 'Jupiter', jlUSDG: 'Jupiter', jlSOL: 'Jupiter',
  kySOL: 'Kyros',
  dSOL: 'Drift', dzSOL: 'Drift', dfdvSOL: 'Drift',
  INF: 'Sanctum',
  rkuSOL: 'Kuru',
  CRT: 'Carrot',
  stORE: 'Ore',
  USDe: 'Ethena', sUSDe: 'Ethena',
  'USD*': 'Perena',
  MLP: 'MarginFi', ALP: 'Adrena',
  syUSDC: 'Maple', syrupUSDC: 'Maple',
  // Per-protocol USDC variants — prefix indicates the issuer.
  'USDC+': 'Reflect',   // Reflect Protocol's stablecoin vault
  mUSDC:   'Marinade',  // Marinade's USDC product
  kUSDC:   'Kyros',     // Kyros's USDC vault
  wkySOL:  'Kyros',     // wrapped kySOL
};

export function platformOfTicker(ticker: string | null | undefined): string {
  if (!ticker) return 'Other';
  return TICKER_TO_PLATFORM[ticker] ?? 'Other';
}

export function colorForPlatform(platform: string | null | undefined): string {
  if (!platform) return FALLBACK;
  return PLATFORM_BASE[platform] ?? FALLBACK;
}

export function colorForTicker(ticker: string | null | undefined): string {
  return colorForPlatform(platformOfTicker(ticker));
}

// Hex shift helpers: returns the same hex with lightness nudged ± `pct`.
// Used to differentiate multiple markets (or multiple maturities of the
// same ticker) within one platform without leaving the hue family.
function hexToHsl(hex: string): [number, number, number] {
  const m = hex.replace('#', '').match(/.{2}/g);
  if (!m) return [0, 0, 0];
  const [r, g, b] = m.map(x => parseInt(x, 16) / 255);
  const mx = Math.max(r, g, b), mn = Math.min(r, g, b);
  const l = (mx + mn) / 2;
  if (mx === mn) return [0, 0, l];
  const d = mx - mn;
  const s = l > 0.5 ? d / (2 - mx - mn) : d / (mx + mn);
  let h = 0;
  if (mx === r) h = ((g - b) / d) + (g < b ? 6 : 0);
  else if (mx === g) h = (b - r) / d + 2;
  else h = (r - g) / d + 4;
  return [h * 60, s, l];
}

function hslToHex(h: number, s: number, l: number): string {
  const c = (1 - Math.abs(2 * l - 1)) * s;
  const x = c * (1 - Math.abs(((h / 60) % 2) - 1));
  const m = l - c / 2;
  let r = 0, g = 0, b = 0;
  if (h < 60) { r = c; g = x; }
  else if (h < 120) { r = x; g = c; }
  else if (h < 180) { g = c; b = x; }
  else if (h < 240) { g = x; b = c; }
  else if (h < 300) { r = x; b = c; }
  else { r = c; b = x; }
  const toHex = (v: number) => Math.round((v + m) * 255).toString(16).padStart(2, '0');
  return `#${toHex(r)}${toHex(g)}${toHex(b)}`;
}

function shadeHex(hex: string, lightnessDelta: number): string {
  const [h, s, l] = hexToHsl(hex);
  // Floor at 0.35 so shades never go near-black on a dark theme;
  // ceil at 0.85 so they don't wash out to white.
  const newL = Math.max(0.35, Math.min(0.85, l + lightnessDelta));
  return hslToHex(h, s, newL);
}

// Color for a specific market within a platform — `i` is the market's
// index among siblings sharing the same platform (so different markets
// of one ticker get different shades). i=0 → base, then alternating
// darker/lighter shades.
export function colorForMarket(platform: string, i: number): string {
  const base = colorForPlatform(platform);
  if (i === 0) return base;
  // Step pattern: -0.08, +0.10, -0.16, +0.20, -0.24 … keeps shades within hue
  const step = (Math.floor((i + 1) / 2)) * 0.08 * (i % 2 === 1 ? -1 : 1);
  return shadeHex(base, step);
}
