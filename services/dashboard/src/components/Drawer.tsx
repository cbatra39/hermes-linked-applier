/**
 * Edge-anchored slide-over panel, rendered into document.body.
 *
 * Used for the record inspectors (a job, a run, a container) where the list
 * behind the panel is still useful context - unlike Modal, which dims the whole
 * page and demands an answer.
 */

import { useRef, type ReactNode, type RefObject } from 'react';
import { createPortal } from 'react-dom';

import { cx } from '../lib/format';
import { useDomId } from '../lib/hooks';
import { IconButton } from './Button';
import { IconClose } from './icons';
import { useBodyScrollLock, useEscapeKey, useFocusTrap } from './overlay';

export type DrawerSide = 'right' | 'bottom';
export type DrawerSize = 'sm' | 'md' | 'lg' | 'xl';

const RIGHT_SIZE: Record<DrawerSize, string> = {
  sm: 'sm:max-w-md',
  md: 'sm:max-w-xl',
  lg: 'sm:max-w-3xl',
  xl: 'sm:max-w-5xl',
};

const BOTTOM_SIZE: Record<DrawerSize, string> = {
  sm: 'max-h-[45vh]',
  md: 'max-h-[65vh]',
  lg: 'max-h-[80vh]',
  xl: 'max-h-[92vh]',
};

export interface DrawerProps {
  open: boolean;
  onClose: () => void;
  title?: ReactNode;
  subtitle?: ReactNode;
  children?: ReactNode;
  /** Header controls, left of the close button. */
  actions?: ReactNode;
  footer?: ReactNode;
  side?: DrawerSide;
  size?: DrawerSize;
  closeOnBackdrop?: boolean;
  initialFocusRef?: RefObject<HTMLElement>;
  className?: string;
  bodyClassName?: string;
}

export function Drawer({
  open,
  onClose,
  title,
  subtitle,
  children,
  actions,
  footer,
  side = 'right',
  size = 'md',
  closeOnBackdrop = true,
  initialFocusRef,
  className,
  bodyClassName,
}: DrawerProps) {
  const panelRef = useRef<HTMLDivElement>(null);
  const titleId = useDomId('drawer-title');

  useBodyScrollLock(open);
  useEscapeKey(open, onClose);
  useFocusTrap(open, panelRef, initialFocusRef);

  if (!open) return null;

  const anchored =
    side === 'right'
      ? cx('inset-y-0 right-0 h-full w-full animate-fade-in border-l', RIGHT_SIZE[size])
      : cx('inset-x-0 bottom-0 w-full animate-slide-up border-t', BOTTOM_SIZE[size]);

  return createPortal(
    <div className="fixed inset-0 z-50">
      <div
        className="absolute inset-0 animate-fade-in bg-ink-950/70"
        onClick={closeOnBackdrop ? onClose : undefined}
        aria-hidden="true"
      />
      <div
        ref={panelRef}
        role="dialog"
        aria-modal="true"
        aria-labelledby={title ? titleId : undefined}
        tabIndex={-1}
        className={cx(
          'absolute flex flex-col border-ink-700 bg-ink-900 shadow-drawer outline-none',
          anchored,
          className,
        )}
      >
        <header className="flex shrink-0 items-start justify-between gap-3 border-b border-ink-750 bg-ink-875 px-4 py-3">
          <div className="min-w-0">
            {title ? (
              <h2 id={titleId} className="truncate text-sm font-semibold text-ink-100">
                {title}
              </h2>
            ) : null}
            {subtitle ? <p className="mt-0.5 truncate text-xs text-ink-400">{subtitle}</p> : null}
          </div>
          <div className="flex shrink-0 items-center gap-2">
            {actions}
            <IconButton label="Close panel" icon={<IconClose size={16} />} onClick={onClose} />
          </div>
        </header>

        <div className={cx('min-h-0 flex-1 overflow-y-auto px-4 py-4', bodyClassName)}>{children}</div>

        {footer ? (
          <footer className="flex shrink-0 flex-wrap items-center justify-end gap-2 border-t border-ink-750 bg-ink-875 px-4 py-3">
            {footer}
          </footer>
        ) : null}
      </div>
    </div>,
    document.body,
  );
}
