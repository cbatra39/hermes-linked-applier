/**
 * StatusSelect - the application-status dropdown on the Jobs page.
 *
 * This is the one control in Hermes that records a real-world action: the user
 * submits an application on LinkedIn themselves, then marks it "applied" here.
 * Hermes cannot observe that (the MCP server has no apply capability and no way
 * to read application state), so this dropdown is the only source of truth for
 * pipeline progress - which is why it is a plain, obvious native <select>
 * rather than a custom widget.
 */

import { JOB_STATUSES, type JobStatus } from '../lib/types';
import { cx } from '../lib/format';

export interface StatusSelectProps {
  value: JobStatus;
  onChange: (next: JobStatus) => void;
  disabled?: boolean;
  className?: string;
  /** Accessible label when the select has no visible <label>. */
  ariaLabel?: string;
}

const STATUS_LABEL: Record<JobStatus, string> = {
  new: 'New',
  shortlisted: 'Shortlisted',
  tailored: 'Resume tailored',
  applied: 'Applied',
  rejected: 'Rejected',
  skipped: 'Skipped',
};

/** Left border tint so status is scannable down a long table. */
const STATUS_ACCENT: Record<JobStatus, string> = {
  new: 'border-l-ink-500',
  shortlisted: 'border-l-info-400',
  tailored: 'border-l-brand-400',
  applied: 'border-l-good-500',
  rejected: 'border-l-bad-500',
  skipped: 'border-l-ink-600',
};

export function StatusSelect({
  value,
  onChange,
  disabled = false,
  className,
  ariaLabel = 'Application status',
}: StatusSelectProps) {
  return (
    <select
      value={value}
      disabled={disabled}
      aria-label={ariaLabel}
      onChange={(event) => onChange(event.target.value as JobStatus)}
      className={cx(
        'rounded border border-l-2 border-ink-600 bg-ink-800 px-2 py-1 text-xs text-ink-100',
        'transition-colors hover:border-ink-500',
        'focus:outline-none focus-visible:ring-2 focus-visible:ring-brand-400',
        'disabled:cursor-not-allowed disabled:opacity-50',
        STATUS_ACCENT[value] ?? 'border-l-ink-500',
        className,
      )}
    >
      {JOB_STATUSES.map((status) => (
        <option key={status} value={status} className="bg-ink-800 text-ink-100">
          {STATUS_LABEL[status] ?? status}
        </option>
      ))}
    </select>
  );
}

export default StatusSelect;
