/**
 * Failure panel. Shows the *actual* message from ApiError (via errorMessage())
 * rather than a friendly lie, because in a self-hosted tool the operator and the
 * user are the same person and they need the real reason.
 */

import type { ReactNode } from 'react';

import { cx } from '../lib/format';
import { Button } from './Button';
import { IconRefresh, IconWarning } from './icons';

export interface ErrorStateProps {
  /** The raw message. Pass `errorMessage(err)` from lib/api. */
  message: string;
  title?: string;
  /** Extra guidance: which container to check, which setting to fill in. */
  hint?: ReactNode;
  onRetry?: () => void;
  retryLabel?: string;
  compact?: boolean;
  className?: string;
}

export function ErrorState({
  message,
  title = 'Something failed',
  hint,
  onRetry,
  retryLabel = 'Try again',
  compact = false,
  className,
}: ErrorStateProps) {
  return (
    <div
      role="alert"
      className={cx(
        'flex items-start gap-3 rounded-lg border border-bad-600/45 bg-bad-600/10',
        compact ? 'px-3 py-2.5' : 'px-4 py-4',
        className,
      )}
    >
      <IconWarning size={compact ? 16 : 18} className="mt-0.5 shrink-0 text-bad-400" />
      <div className="min-w-0 flex-1">
        <p className={cx('font-medium text-bad-400', compact ? 'text-xs' : 'text-sm')}>{title}</p>
        <p className="mt-1 break-words font-mono text-xs leading-5 text-ink-200">{message}</p>
        {hint ? <p className="mt-2 text-xs leading-relaxed text-ink-300">{hint}</p> : null}
        {onRetry ? (
          <Button
            variant="secondary"
            size="sm"
            className="mt-3"
            icon={<IconRefresh size={14} />}
            onClick={onRetry}
          >
            {retryLabel}
          </Button>
        ) : null}
      </div>
    </div>
  );
}

/** Non-blocking warning strip - a partial failure that did not kill the page. */
export function WarningNote({ children, className }: { children: ReactNode; className?: string }) {
  return (
    <div
      className={cx(
        'flex items-start gap-2.5 rounded-md border border-warn-600/40 bg-warn-600/10 px-3 py-2 text-xs leading-relaxed text-warn-400',
        className,
      )}
    >
      <IconWarning size={14} className="mt-0.5 shrink-0" />
      <div className="min-w-0 text-ink-200">{children}</div>
    </div>
  );
}
