// Tranche APY projector. See docs/ONYC_TRANCHES_PLAN.md for the derivation
// chain (workflows on 2026-06-27) and the open caveats.
//
// Verified at our current ONyc state to 4 decimals:
//   utilization = min_coverage / coverage   (98% confidence)
//   sr_apy ≈ underlying × (1 − jr_share)    (85%, conservation identity)
//   jr_apy ≈ underlying × (1 + jr_share × sr/jr)  (85%, derived from above)
//
// The SDK does NOT ship these closed forms — API computes APY from realized
// NAV growth. Projector outputs match in steady state and diverge during
// IL events / fee accrual. Treat as a what-if calculator, not a guarantee.

export type CurvePoint = { x: number; y: number };

export type TrancheProjection = {
  juniorReturnShare: number;
  seniorApy: number;
  juniorApy: number;
  coverageRatio: number;
};

/** Linearly interpolate y from a sorted-by-x array of curve points. Clamps
 *  at the endpoints (no extrapolation). */
export function interpolateCurve(points: CurvePoint[], x: number): number {
  if (!points.length) return 0;
  const sorted = [...points].sort((a, b) => a.x - b.x);
  if (x <= sorted[0].x) return sorted[0].y;
  if (x >= sorted[sorted.length - 1].x) return sorted[sorted.length - 1].y;
  for (let i = 0; i < sorted.length - 1; i++) {
    const a = sorted[i];
    const b = sorted[i + 1];
    if (x >= a.x && x < b.x) {
      const t = (x - a.x) / (b.x - a.x);
      return a.y + t * (b.y - a.y);
    }
  }
  return sorted[sorted.length - 1].y;
}

/** Derive coverage from sizes. coverage = jr_eff / (sr_eff + jr_eff). */
export function coverageFromSizes(srSize: number, jrSize: number): number {
  const total = srSize + jrSize;
  return total > 0 ? jrSize / total : 0;
}

/** Derive utilization (the curve x-input) from coverage + protocol's
 *  min_coverage parameter. utilization = min_coverage / coverage. */
export function utilizationFromCoverage(coverage: number, minCoverage: number): number {
  if (coverage <= 0) return Number.POSITIVE_INFINITY;
  return minCoverage / coverage;
}

export type ProjectInputs = {
  /** Current sr effective NAV in USD (or any scale; only ratio matters). */
  srSize: number;
  /** Current jr effective NAV in USD. */
  jrSize: number;
  /** Underlying yield rate (e.g. wONyc SY rate annualized). 0.1178 = 11.78%. */
  underlyingApy: number;
  /** Protocol's min coverage param (read from raw_tranche_states). 0.2 for ONyc. */
  minCoverage: number;
  /** Piecewise-linear return curve. 50 (x, y) points for ONyc. */
  curve: CurvePoint[];
};

/** Project senior + junior APYs at the given sr/jr sizes.
 *
 * Pipeline:
 *   1. coverage = jr / (sr + jr)
 *   2. utilization = min_coverage / coverage
 *   3. jr_share = interpolate(curve, utilization)   ← clamps at curve endpoints
 *   4. sr_apy = underlying × (1 − jr_share)
 *   5. jr_apy = underlying × (1 + jr_share × sr/jr)
 *
 * Returns NaN-free values; junior_apy can grow very large when jr_size is
 * small (matches the "infinite leverage" edge case in the protocol design). */
export function projectTrancheApy(inputs: ProjectInputs): TrancheProjection {
  const { srSize, jrSize, underlyingApy, minCoverage, curve } = inputs;
  const coverage = coverageFromSizes(srSize, jrSize);
  const util = utilizationFromCoverage(coverage, minCoverage);
  const jrShare = interpolateCurve(curve, util);
  const seniorApy = underlyingApy * (1 - jrShare);
  const juniorApy = jrSize > 0
    ? underlyingApy * (1 + jrShare * (srSize / jrSize))
    : Number.POSITIVE_INFINITY;
  return {
    juniorReturnShare: jrShare,
    seniorApy,
    juniorApy,
    coverageRatio: coverage,
  };
}
