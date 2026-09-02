/** "Nothing here yet" placeholder. Always tells the user what to do next. */

import type { ReactNode } from 'react';

import { cx } from '../lib/format';
import { IconInbox } from './icons';

export interface EmptyStateProps {
  title: string;
  /** What to do about it. Keep it to one sentence. */
  description?: ReactNode;
  /** Alias of `description`, used by the Jobs page. */
  message?: ReactNode;
  /** A primary action - usually the button that creates the missing thing. */
  action?: ReactNode;
  icon?: ReactNode;
  compact?: boolean;
  className?: string;
}

export function EmptyState({ title, description, message, action, icon, compact = false, className }: EmptyStateProps) {
  const body = description ?? message;
  return (
    <div
      className={cx(
        'flex flex-col items-center justify-center text-center',
        compact ? 'gap-2 px-4 py-8' : 'gap-3 px-6 py-14',
        className,
      )}
    >
      <div className="rounded-full border border-ink-750 bg-ink-850 p-3 text-ink-400">
        {icon ?? <IconInbox size={compact ? 18 : 22} />}
      </div>
      <div>
        <p className={cx('font-medium text-ink-200', compact ? 'text-sm' : 'text-base')}>{title}</p>
        {body ? (
          <p className="mx-auto mt-1 max-w-md text-sm leading-relaxed text-ink-400">{body}</p>
        ) : null}
      </div>
      {action ? <div className="mt-1 flex flex-wrap justify-center gap-2">{action}</div> : null}
    </div>
  );
}
