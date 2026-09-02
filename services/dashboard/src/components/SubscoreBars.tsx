/**
 * SubscoreBars - the six weighted ATS subscores as proportional bars.
 *
 * Each subscore has a different maximum (keyword coverage is worth 25 points,
 * contact block only 10), so a raw value is meaningless on its own: 9/10 and
 * 9/25 would draw the same bar. Every row is therefore normalised against
 * ATS_SUBSCORE_MAX from lib/types.ts, which mirrors WEIGHTS in agents/ats.py,
 * and the earned/possible split is shown as text next to the bar.
 */

import { ATS_SUBSCORE_LABELS, ATS_SUBSCORE_MAX, type AtsSubscores } from '../lib/types';
import { cx } from '../lib/format';

export interface SubscoreBarsProps {
  subscores?: AtsSubscores | null;
  className?: string;
}

function barColor(ratio: number): string {
  if (ratio >= 0.85) return 'bg-good-500';
  if (ratio >= 0.6) return 'bg-brand-400';
  if (ratio >= 0.35) return 'bg-warn-500';
  return 'bg-bad-500';
}

export function SubscoreBars({ subscores, className }: SubscoreBarsProps) {
  const entries = Object.entries(subscores ?? {}).filter(
    ([, value]) => typeof value === 'number' && Number.isFinite(value),
  ) as Array<[string, number]>;

  if (entries.length === 0) {
    return (
      <p className={cx('text-xs text-ink-400', className)}>
        No subscore breakdown available. Score a resume to populate this.
      </p>
    );
  }

  // Render in the canonical weight order, then anything unexpected the backend
  // added, so the list stays stable between scores instead of reordering.
  const canonical = Object.keys(ATS_SUBSCORE_MAX);
  entries.sort(([a], [b]) => {
    const ia = canonical.indexOf(a);
    const ib = canonical.indexOf(b);
    if (ia === -1 && ib === -1) return a.localeCompare(b);
    if (ia === -1) return 1;
    if (ib === -1) return -1;
    return ia - ib;
  });

  return (
    <ul className={cx('flex flex-col gap-2.5', className)}>
      {entries.map(([key, value]) => {
        const max = ATS_SUBSCORE_MAX[key] ?? 100;
        const ratio = max > 0 ? Math.max(0, Math.min(1, value / max)) : 0;
        const label = ATS_SUBSCORE_LABELS[key] ?? key.replace(/_/g, ' ');
        const pct = Math.round(ratio * 100);

        return (
          <li key={key}>
            <div className="flex items-baseline justify-between gap-3">
              <span className="text-xs font-medium capitalize text-ink-200">{label}</span>
              <span className="text-2xs tabular-nums text-ink-400">
                {Number(value.toFixed(1))} / {max}
              </span>
            </div>
            <div
              className="mt-1 h-1.5 w-full overflow-hidden rounded-full bg-ink-750"
              role="meter"
              aria-valuenow={pct}
              aria-valuemin={0}
              aria-valuemax={100}
              aria-label={`${label}: ${pct}% of available points`}
            >
              <div
                className={cx('h-full rounded-full transition-all duration-500', barColor(ratio))}
                style={{ width: `${pct}%` }}
              />
            </div>
          </li>
        );
      })}
    </ul>
  );
}

export default SubscoreBars;
