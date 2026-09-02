/** Small status pills, plus the tone lookups that keep colours consistent. */

import type { ReactNode } from 'react';

import { cx, scoreTone, type ScoreTone } from '../lib/format';
import type { JobStatus, MatchVerdict, RunStatus } from '../lib/types';

export type BadgeTone = 'neutral' | 'brand' | 'good' | 'warn' | 'bad' | 'info';

const TONE: Record<BadgeTone, string> = {
  neutral: 'bg-ink-750 text-ink-200 ring-ink-600',
  brand: 'bg-brand-800/50 text-brand-200 ring-brand-600/60',
  good: 'bg-good-600/15 text-good-400 ring-good-600/45',
  warn: 'bg-warn-600/15 text-warn-400 ring-warn-600/45',
  bad: 'bg-bad-600/15 text-bad-400 ring-bad-600/45',
  info: 'bg-info-500/15 text-info-400 ring-info-500/45',
};

const DOT_TONE: Record<BadgeTone, string> = {
  neutral: 'bg-ink-400',
  brand: 'bg-brand-400',
  good: 'bg-good-400',
  warn: 'bg-warn-400',
  bad: 'bg-bad-400',
  info: 'bg-info-400',
};

export interface BadgeProps {
  children: ReactNode;
  tone?: BadgeTone;
  /** Leading filled dot - handy in a dense table cell. */
  dot?: boolean;
  /** Native tooltip, e.g. the full failure detail behind a short label. */
  title?: string;
  className?: string;
}

export function Badge({ children, tone = 'neutral', dot = false, title, className }: BadgeProps) {
  return (
    <span
      title={title}
      className={cx(
        'inline-flex max-w-full items-center gap-1.5 rounded-full px-2 py-0.5 text-2xs font-medium ring-1 ring-inset',
        TONE[tone],
        className,
      )}
    >
      {dot ? <span className={cx('h-1.5 w-1.5 shrink-0 rounded-full', DOT_TONE[tone])} aria-hidden="true" /> : null}
      <span className="truncate">{children}</span>
    </span>
  );
}

/* -------------------------------------------------------------------------- */
/* Tone lookups                                                               */
/* -------------------------------------------------------------------------- */

/**
 * Job pipeline colour. `applied` is deliberately brand-coloured, not green: the
 * human clicked the apply link themselves, and Hermes cannot verify an outcome.
 */
export function jobStatusTone(status: JobStatus | string | null | undefined): BadgeTone {
  switch (status) {
    case 'shortlisted':
      return 'info';
    case 'tailored':
      return 'good';
    case 'applied':
      return 'brand';
    case 'rejected':
      return 'bad';
    case 'skipped':
      return 'neutral';
    case 'new':
    default:
      return 'neutral';
  }
}

export function runStatusTone(status: RunStatus | string | null | undefined): BadgeTone {
  switch (status) {
    case 'running':
      return 'info';
    case 'done':
      return 'good';
    case 'error':
      return 'bad';
    case 'pending':
      return 'warn';
    default:
      return 'neutral';
  }
}

/** Docker container state (`running`, `exited`, `paused`, ...). */
export function containerStateTone(state: string | null | undefined): BadgeTone {
  const value = (state ?? '').toLowerCase();
  if (value.includes('running') || value.includes('up')) return 'good';
  if (value.includes('restart')) return 'warn';
  if (value.includes('paused') || value.includes('created')) return 'warn';
  if (value.includes('dead') || value.includes('exit')) return 'bad';
  return 'neutral';
}

export function verdictTone(verdict: MatchVerdict | string | null | undefined): BadgeTone {
  switch (verdict) {
    case 'strong':
      return 'good';
    case 'good':
      return 'info';
    case 'stretch':
      return 'warn';
    case 'poor':
      return 'bad';
    default:
      return 'neutral';
  }
}

const SCORE_TONE_TO_BADGE: Record<ScoreTone, BadgeTone> = {
  good: 'good',
  ok: 'warn',
  weak: 'bad',
  none: 'neutral',
};

export function scoreBadgeTone(score: number | null | undefined): BadgeTone {
  return SCORE_TONE_TO_BADGE[scoreTone(score)];
}

/** A 0-100 score as a colour-graded pill. Renders "-" when the score is absent. */
export function ScoreBadge({
  score,
  suffix = '',
  title,
  className,
}: {
  score: number | null | undefined;
  suffix?: string;
  title?: string;
  className?: string;
}) {
  const absent = score === null || score === undefined || Number.isNaN(score);
  return (
    <Badge tone={scoreBadgeTone(score)} title={title} className={cx('nums', className)}>
      {absent ? '-' : `${Math.round(score)}${suffix}`}
    </Badge>
  );
}
