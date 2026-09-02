/**
 * ScoreGauge - a 0-100 donut, drawn as inline SVG.
 *
 * Inline SVG rather than a charting library: this is one arc, and the stack
 * ships no chart dependency. `stroke-dasharray` on a circle gives an exact arc
 * with no path maths.
 *
 * The caption is deliberately part of the component API. Every score Hermes
 * shows is a heuristic proxy, and a bare number in a big ring reads as
 * authoritative - the caption is where that gets qualified.
 */

import { cx } from '../lib/format';

export type ScoreGaugeSize = 'sm' | 'md' | 'lg';

export interface ScoreGaugeProps {
  /** 0-100. `null`/`undefined` renders an explicit "not scored yet" state. */
  score?: number | null;
  label?: string;
  /** Small print under the number - use it to qualify what the score means. */
  caption?: string;
  size?: ScoreGaugeSize;
  className?: string;
}

const GEOMETRY: Record<ScoreGaugeSize, { box: number; stroke: number; value: string; label: string }> = {
  sm: { box: 72, stroke: 7, value: 'text-base', label: 'text-2xs' },
  md: { box: 112, stroke: 9, value: 'text-2xl', label: 'text-xs' },
  lg: { box: 148, stroke: 11, value: 'text-3xl', label: 'text-sm' },
};

/** Colour bands. Kept in step with the verdict bands in agents/match_ranker.py. */
function bandColor(score: number): string {
  if (score >= 78) return 'var(--hermes-good)';
  if (score >= 58) return 'var(--hermes-brand)';
  if (score >= 38) return 'var(--hermes-warn)';
  return 'var(--hermes-bad)';
}

export function ScoreGauge({ score, label, caption, size = 'md', className }: ScoreGaugeProps) {
  const { box, stroke, value: valueClass, label: labelClass } = GEOMETRY[size];
  const scored = typeof score === 'number' && Number.isFinite(score);
  const clamped = scored ? Math.max(0, Math.min(100, score as number)) : 0;

  const radius = (box - stroke) / 2;
  const circumference = 2 * Math.PI * radius;
  const filled = (clamped / 100) * circumference;
  const center = box / 2;

  return (
    <div className={cx('flex flex-col items-center gap-1.5', className)}>
      <div className="relative" style={{ width: box, height: box }}>
        <svg
          width={box}
          height={box}
          viewBox={`0 0 ${box} ${box}`}
          role="img"
          aria-label={
            scored
              ? `${label ?? 'Score'}: ${Math.round(clamped)} out of 100`
              : `${label ?? 'Score'}: not scored yet`
          }
          className="-rotate-90"
        >
          <circle
            cx={center}
            cy={center}
            r={radius}
            fill="none"
            stroke="var(--hermes-border)"
            strokeWidth={stroke}
          />
          {scored && (
            <circle
              cx={center}
              cy={center}
              r={radius}
              fill="none"
              stroke={bandColor(clamped)}
              strokeWidth={stroke}
              strokeLinecap="round"
              strokeDasharray={`${filled} ${circumference - filled}`}
              style={{ transition: 'stroke-dasharray 420ms ease-out' }}
            />
          )}
        </svg>
        <div className="absolute inset-0 flex flex-col items-center justify-center">
          {scored ? (
            <span className={cx('font-semibold tabular-nums text-ink-100', valueClass)}>
              {Math.round(clamped)}
            </span>
          ) : (
            <span className={cx('font-medium text-ink-400', labelClass)}>--</span>
          )}
        </div>
      </div>

      {label && <span className={cx('font-medium text-ink-300', labelClass)}>{label}</span>}
      {caption && <span className="text-2xs text-center text-ink-400">{caption}</span>}
      {!scored && !caption && <span className="text-2xs text-ink-400">Not scored yet</span>}
    </div>
  );
}

export default ScoreGauge;
