/** Indeterminate activity indicator. Inline SVG so it inherits `currentColor`. */

import { cx } from '../lib/format';

/** Named sizes in px, so callers can pass either a token or a raw number. */
const SPINNER_PX = { sm: 14, md: 16, lg: 22 } as const;

export interface SpinnerProps {
  /** Rendered square size: a px number, or one of 'sm' | 'md' | 'lg'. */
  size?: number | keyof typeof SPINNER_PX;
  className?: string;
  /**
   * Screen-reader text. Defaults to "Loading". Pass `label=""` when a nearby
   * visible label already announces the wait, to avoid a double announcement.
   */
  label?: string;
}

export function Spinner({ size = 16, className, label = 'Loading' }: SpinnerProps) {
  const px = typeof size === 'number' ? size : SPINNER_PX[size];
  return (
    <span className={cx('inline-flex items-center', className)}>
      <svg
        width={px}
        height={px}
        viewBox="0 0 24 24"
        fill="none"
        className="animate-spin"
        aria-hidden="true"
        focusable="false"
      >
        <circle cx="12" cy="12" r="9.2" stroke="currentColor" strokeWidth="2.6" opacity="0.22" />
        <path
          d="M21.2 12a9.2 9.2 0 0 0-9.2-9.2"
          stroke="currentColor"
          strokeWidth="2.6"
          strokeLinecap="round"
        />
      </svg>
      {label ? <span className="sr-only">{label}</span> : null}
    </span>
  );
}

/** Centred spinner + caption, for a panel that is loading its first payload. */
export function LoadingBlock({ label = 'Loading', className }: { label?: string; className?: string }) {
  return (
    <div
      className={cx('flex items-center justify-center gap-3 px-4 py-12 text-sm text-ink-300', className)}
      role="status"
      aria-live="polite"
    >
      <Spinner size={18} label="" />
      <span>{label}</span>
    </div>
  );
}

/** Shimmering placeholder bar, for skeleton rows in a table or card. */
export function SkeletonBar({ className }: { className?: string }) {
  return <div className={cx('h-3 animate-pulseline rounded bg-ink-700', className)} aria-hidden="true" />;
}
