/**
 * KeywordChips - a wrapped list of keyword pills.
 *
 * Used in two opposite senses, which is why `tone` is required rather than
 * cosmetic: green chips are keywords the candidate demonstrably has, red chips
 * are gaps the job asked for and the profile did not evidence. Mixing those up
 * would invert the meaning of the Resume and Jobs pages, so the tone also
 * drives the screen-reader prefix.
 */

import { useState } from 'react';

import { cx } from '../lib/format';

export type KeywordTone = 'good' | 'bad' | 'neutral' | 'brand' | 'warn';

export interface KeywordChipsProps {
  items?: readonly string[] | null;
  tone?: KeywordTone;
  emptyText?: string;
  /** Chips shown before collapsing behind a "+N more" toggle. */
  max?: number;
  className?: string;
}

const TONE: Record<KeywordTone, string> = {
  good: 'border-good-600/40 bg-good-600/10 text-good-400',
  bad: 'border-bad-600/40 bg-bad-600/10 text-bad-400',
  neutral: 'border-ink-600 bg-ink-800 text-ink-200',
  brand: 'border-brand-600/40 bg-brand-600/10 text-brand-200',
  warn: 'border-warn-600/40 bg-warn-600/10 text-warn-400',
};

const TONE_A11Y: Record<KeywordTone, string> = {
  good: 'matched keyword',
  bad: 'missing keyword',
  neutral: 'keyword',
  brand: 'keyword',
  warn: 'keyword to strengthen',
};

export function KeywordChips({
  items,
  tone = 'neutral',
  emptyText = 'None.',
  max = 40,
  className,
}: KeywordChipsProps) {
  const [expanded, setExpanded] = useState(false);

  const clean = (items ?? [])
    .map((item) => String(item ?? '').trim())
    .filter((item) => item.length > 0);

  // The same keyword can arrive twice (once from the deterministic scorer, once
  // from the LLM pass); de-duplicate case-insensitively but keep first casing.
  const seen = new Set<string>();
  const unique = clean.filter((item) => {
    const key = item.toLowerCase();
    if (seen.has(key)) return false;
    seen.add(key);
    return true;
  });

  if (unique.length === 0) {
    return <p className={cx('text-xs text-ink-400', className)}>{emptyText}</p>;
  }

  const visible = expanded ? unique : unique.slice(0, max);
  const hidden = unique.length - visible.length;

  return (
    <div className={cx('flex flex-wrap items-center gap-1.5', className)}>
      {visible.map((item) => (
        <span
          key={item}
          className={cx(
            'inline-flex max-w-full items-center truncate rounded-full border px-2 py-0.5 text-2xs font-medium',
            TONE[tone],
          )}
          title={`${TONE_A11Y[tone]}: ${item}`}
        >
          {item}
        </span>
      ))}
      {hidden > 0 && (
        <button
          type="button"
          onClick={() => setExpanded(true)}
          className="rounded-full border border-ink-600 px-2 py-0.5 text-2xs font-medium text-ink-300 transition-colors hover:border-ink-500 hover:text-ink-100 focus:outline-none focus-visible:ring-2 focus-visible:ring-brand-400"
        >
          +{hidden} more
        </button>
      )}
      {expanded && unique.length > max && (
        <button
          type="button"
          onClick={() => setExpanded(false)}
          className="rounded-full border border-ink-600 px-2 py-0.5 text-2xs font-medium text-ink-300 transition-colors hover:border-ink-500 hover:text-ink-100 focus:outline-none focus-visible:ring-2 focus-visible:ring-brand-400"
        >
          Show fewer
        </button>
      )}
    </div>
  );
}

export default KeywordChips;
