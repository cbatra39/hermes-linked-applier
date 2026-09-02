/** Panels, section headers and metric tiles - the page furniture. */

import type { ReactNode } from 'react';

import { cx } from '../lib/format';
import { Spinner } from './Spinner';
import { canonStatusTone, type CanonStatusTone, type StatusTone } from './StatusDot';

export interface CardProps {
  title?: ReactNode;
  subtitle?: ReactNode;
  /** Right-aligned controls in the header row. */
  actions?: ReactNode;
  footer?: ReactNode;
  children?: ReactNode;
  /** Shows a spinner in the header - for a background refresh, not a first load. */
  busy?: boolean;
  /** Set false when the body owns its own padding (a table, a log stream). */
  padded?: boolean;
  className?: string;
  bodyClassName?: string;
  id?: string;
  /** Renders as <section aria-labelledby> when a heading id is supplied. */
  headingId?: string;
}

export function Card({
  title,
  subtitle,
  actions,
  footer,
  children,
  busy = false,
  padded = true,
  className,
  bodyClassName,
  id,
  headingId,
}: CardProps) {
  const hasHeader = Boolean(title || subtitle || actions || busy);

  return (
    <section
      id={id}
      aria-labelledby={headingId}
      className={cx(
        'flex min-w-0 flex-col rounded-lg border border-ink-750 bg-ink-875 shadow-panel',
        className,
      )}
    >
      {hasHeader ? (
        <header className="flex flex-wrap items-start justify-between gap-3 border-b border-ink-750 px-4 py-3">
          <div className="min-w-0">
            {title ? (
              <h2 id={headingId} className="flex items-center gap-2 text-sm font-semibold text-ink-100">
                <span className="truncate">{title}</span>
                {busy ? <Spinner size={13} className="text-ink-400" label="Refreshing" /> : null}
              </h2>
            ) : null}
            {subtitle ? <p className="mt-0.5 text-xs text-ink-400">{subtitle}</p> : null}
          </div>
          {actions ? <div className="flex shrink-0 flex-wrap items-center gap-2">{actions}</div> : null}
        </header>
      ) : null}

      <div className={cx('min-w-0 flex-1', padded && 'px-4 py-4', bodyClassName)}>{children}</div>

      {footer ? (
        <footer className="border-t border-ink-750 px-4 py-2.5 text-xs text-ink-400">{footer}</footer>
      ) : null}
    </section>
  );
}

/* -------------------------------------------------------------------------- */
/* Page header                                                                */
/* -------------------------------------------------------------------------- */

export interface PageHeaderProps {
  title: string;
  description?: ReactNode;
  actions?: ReactNode;
  className?: string;
}

/** The h1 + blurb + primary actions row at the top of every page. */
export function PageHeader({ title, description, actions, className }: PageHeaderProps) {
  return (
    <div className={cx('mb-5 flex flex-wrap items-end justify-between gap-4', className)}>
      <div className="min-w-0">
        <h1 className="text-xl font-semibold tracking-tight text-ink-100">{title}</h1>
        {description ? <p className="mt-1 max-w-3xl text-sm text-ink-300">{description}</p> : null}
      </div>
      {actions ? <div className="flex flex-wrap items-center gap-2">{actions}</div> : null}
    </div>
  );
}

/* -------------------------------------------------------------------------- */
/* Metric tile                                                                */
/* -------------------------------------------------------------------------- */

const STAT_ACCENT: Record<CanonStatusTone, string> = {
  ok: 'text-good-400',
  warn: 'text-warn-400',
  bad: 'text-bad-400',
  busy: 'text-brand-300',
  info: 'text-info-400',
  unknown: 'text-ink-100',
};

export interface StatProps {
  label: ReactNode;
  value: ReactNode;
  /** Small caption under the value: a delta, a unit, a timestamp. */
  hint?: ReactNode;
  /** Colours the value. Default `unknown` = plain ink. */
  tone?: StatusTone;
  icon?: ReactNode;
  className?: string;
}

/** Single KPI tile. Overview lays several of these in a grid. */
export function Stat({ label, value, hint, tone = 'unknown', icon, className }: StatProps) {
  return (
    <div
      className={cx(
        'flex min-w-0 items-start justify-between gap-3 rounded-lg border border-ink-750 bg-ink-875 px-4 py-3 shadow-panel',
        className,
      )}
    >
      <div className="min-w-0">
        <div className="truncate text-2xs font-medium uppercase tracking-wide text-ink-400">{label}</div>
        <div className={cx('nums mt-1 text-2xl font-semibold leading-none', STAT_ACCENT[canonStatusTone(tone)])}>{value}</div>
        {hint ? <div className="mt-1.5 truncate text-xs text-ink-400">{hint}</div> : null}
      </div>
      {icon ? <div className="shrink-0 rounded-md bg-ink-800 p-2 text-ink-300">{icon}</div> : null}
    </div>
  );
}

/* -------------------------------------------------------------------------- */
/* Key/value list                                                             */
/* -------------------------------------------------------------------------- */

export interface FieldRow {
  label: ReactNode;
  value: ReactNode;
  /** Renders the value in the monospace stack (ids, paths, urls). */
  mono?: boolean;
}

/** Definition list for detail panels (job meta, container meta, run params). */
export function FieldList({ rows, className }: { rows: readonly FieldRow[]; className?: string }) {
  if (!rows.length) return null;
  return (
    <dl className={cx('grid gap-x-4 gap-y-2.5 sm:grid-cols-[minmax(7rem,auto)_1fr]', className)}>
      {rows.map((row, index) => (
        <div key={index} className="contents">
          <dt className="text-xs font-medium uppercase tracking-wide text-ink-400 sm:pt-0.5">{row.label}</dt>
          <dd className={cx('min-w-0 text-sm text-ink-100', row.mono && 'break-all font-mono text-xs')}>
            {row.value}
          </dd>
        </div>
      ))}
    </dl>
  );
}
