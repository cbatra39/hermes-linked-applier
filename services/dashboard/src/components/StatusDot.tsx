/**
 * Traffic-light dot for a dependency's health (LLM router, LinkedIn MCP, Docker).
 *
 * `unknown` is a distinct state from `bad` on purpose: "we have not asked yet"
 * must not look like "it is broken", or the first paint of the header lies.
 *
 * Tone vocabulary: the pages were written against two overlapping spellings —
 * the dot's own (`ok`/`unknown`/`busy`) and the badge palette's
 * (`good`/`neutral`/`brand`/`info`). Rather than force one on every call site,
 * `StatusTone` accepts both and `canonStatusTone` collapses them to the five
 * visual states that actually exist. That also makes
 * `<StatusDot tone={runStatusTone(status)} />` work directly, reusing the
 * status→tone mappings already exported from Badge.tsx.
 */

import type { ReactNode } from 'react';

import { cx } from '../lib/format';

/** The five states this component can actually draw. */
export type CanonStatusTone = 'ok' | 'warn' | 'bad' | 'busy' | 'unknown' | 'info';

/** Everything callers may pass, including the badge-palette aliases. */
export type StatusTone =
  | CanonStatusTone
  | 'good'
  | 'idle'
  | 'neutral'
  | 'brand';

/** Collapse the aliases so the lookup tables below stay exhaustive. */
export function canonStatusTone(tone: StatusTone): CanonStatusTone {
  switch (tone) {
    case 'good':
      return 'ok';
    case 'idle':
    case 'neutral':
      return 'unknown';
    case 'brand':
      return 'busy';
    default:
      return tone;
  }
}

const DOT: Record<CanonStatusTone, string> = {
  ok: 'bg-good-400 shadow-[0_0_0_3px_rgba(34,197,94,0.16)]',
  warn: 'bg-warn-400 shadow-[0_0_0_3px_rgba(245,158,11,0.16)]',
  bad: 'bg-bad-400 shadow-[0_0_0_3px_rgba(239,68,68,0.16)]',
  busy: 'bg-brand-400 shadow-[0_0_0_3px_rgba(46,200,228,0.16)]',
  info: 'bg-info-400 shadow-[0_0_0_3px_rgba(129,140,248,0.16)]',
  unknown: 'bg-ink-500',
};

const TEXT: Record<CanonStatusTone, string> = {
  ok: 'text-ink-200',
  warn: 'text-warn-400',
  bad: 'text-bad-400',
  busy: 'text-brand-300',
  info: 'text-info-400',
  unknown: 'text-ink-400',
};

const FALLBACK_WORD: Record<CanonStatusTone, string> = {
  ok: 'healthy',
  warn: 'degraded',
  bad: 'failing',
  busy: 'checking',
  info: 'in progress',
  unknown: 'unknown',
};

export interface StatusDotProps {
  tone?: StatusTone;
  /**
   * Alias of `tone`, accepting a raw run/container status string. Kept because
   * the Runs table passes the status straight through; unrecognised values fall
   * back to `unknown` rather than throwing.
   */
  status?: string;
  /** Visible text beside the dot. Omit for a bare dot inside a table cell. */
  label?: ReactNode;
  /** Native tooltip: put the raw `detail` string from /api/health here. */
  title?: string;
  /** Accessible name. Defaults to "<label>: <tone word>". */
  srLabel?: string;
  size?: 'sm' | 'md';
  /** Force the pulse animation on (a 'busy' tone pulses regardless). */
  pulse?: boolean;
  className?: string;
}

/** Raw status strings the backend uses, mapped to a visual state. */
const STATUS_TONE: Record<string, CanonStatusTone> = {
  // Run.status
  done: 'ok',
  error: 'bad',
  running: 'busy',
  pending: 'unknown',
  // Container state
  created: 'unknown',
  restarting: 'busy',
  exited: 'bad',
  dead: 'bad',
  paused: 'warn',
  removing: 'warn',
};

export function StatusDot({
  tone,
  status,
  label,
  title,
  srLabel,
  size = 'md',
  pulse = false,
  className,
}: StatusDotProps) {
  const key: CanonStatusTone = tone
    ? canonStatusTone(tone)
    : STATUS_TONE[String(status ?? '').toLowerCase()] ?? 'unknown';

  const box = size === 'sm' ? 'h-1.5 w-1.5' : 'h-2 w-2';
  const spoken =
    srLabel ?? `${typeof label === 'string' ? `${label}: ` : ''}${FALLBACK_WORD[key]}`;

  return (
    <span className={cx('inline-flex items-center gap-2', className)} title={title}>
      <span
        className={cx('shrink-0 rounded-full', box, DOT[key], (pulse || key === 'busy') && 'animate-pulseline')}
        role="img"
        aria-label={spoken}
      />
      {label ? (
        <span className={cx('truncate text-xs font-medium', TEXT[key])} aria-hidden="true">
          {label}
        </span>
      ) : null}
    </span>
  );
}
