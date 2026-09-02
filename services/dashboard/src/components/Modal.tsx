/** Centred dialog rendered into document.body. */

import { useRef, type ReactNode, type RefObject } from 'react';
import { createPortal } from 'react-dom';

import { cx } from '../lib/format';
import { useDomId } from '../lib/hooks';
import { IconButton } from './Button';
import { IconClose } from './icons';
import { useBodyScrollLock, useEscapeKey, useFocusTrap } from './overlay';

export type ModalSize = 'sm' | 'md' | 'lg' | 'xl';

const SIZE: Record<ModalSize, string> = {
  sm: 'max-w-sm',
  md: 'max-w-lg',
  lg: 'max-w-2xl',
  xl: 'max-w-4xl',
};

export interface ModalProps {
  open: boolean;
  onClose: () => void;
  title?: ReactNode;
  /** Sub-heading under the title, wired up as aria-describedby. */
  description?: ReactNode;
  children?: ReactNode;
  /** Action row pinned to the bottom of the dialog. */
  footer?: ReactNode;
  size?: ModalSize;
  /** Clicking the dimmed backdrop closes the dialog. Default true. */
  closeOnBackdrop?: boolean;
  /** Hides the corner X (use for a dialog that must be answered). */
  hideCloseButton?: boolean;
  /** Element to focus when the dialog opens. Defaults to the first focusable. */
  initialFocusRef?: RefObject<HTMLElement>;
  className?: string;
  bodyClassName?: string;
}

export function Modal({
  open,
  onClose,
  title,
  description,
  children,
  footer,
  size = 'md',
  closeOnBackdrop = true,
  hideCloseButton = false,
  initialFocusRef,
  className,
  bodyClassName,
}: ModalProps) {
  const panelRef = useRef<HTMLDivElement>(null);
  const titleId = useDomId('modal-title');
  const descId = useDomId('modal-desc');

  useBodyScrollLock(open);
  useEscapeKey(open, onClose);
  useFocusTrap(open, panelRef, initialFocusRef);

  if (!open) return null;

  return createPortal(
    <div className="fixed inset-0 z-50 flex items-start justify-center overflow-y-auto p-4 sm:items-center sm:p-6">
      <div
        className="fixed inset-0 animate-fade-in bg-ink-950/75 backdrop-blur-[2px]"
        onClick={closeOnBackdrop ? onClose : undefined}
        aria-hidden="true"
      />
      <div
        ref={panelRef}
        role="dialog"
        aria-modal="true"
        aria-labelledby={title ? titleId : undefined}
        aria-describedby={description ? descId : undefined}
        // -1 so the trap can park focus on the panel when it holds nothing focusable.
        tabIndex={-1}
        className={cx(
          'relative z-10 my-auto flex w-full animate-slide-up flex-col rounded-xl border border-ink-700 bg-ink-875 shadow-panel outline-none',
          'max-h-[calc(100vh-2rem)] sm:max-h-[calc(100vh-6rem)]',
          SIZE[size],
          className,
        )}
      >
        {title || !hideCloseButton ? (
          <header className="flex items-start justify-between gap-4 border-b border-ink-750 px-5 py-3.5">
            <div className="min-w-0">
              {title ? (
                <h2 id={titleId} className="text-sm font-semibold text-ink-100">
                  {title}
                </h2>
              ) : null}
              {description ? (
                <p id={descId} className="mt-1 text-xs leading-relaxed text-ink-400">
                  {description}
                </p>
              ) : null}
            </div>
            {hideCloseButton ? null : (
              <IconButton label="Close dialog" icon={<IconClose size={16} />} onClick={onClose} />
            )}
          </header>
        ) : null}

        <div className={cx('min-h-0 flex-1 overflow-y-auto px-5 py-4', bodyClassName)}>{children}</div>

        {footer ? (
          <footer className="flex flex-wrap items-center justify-end gap-2 border-t border-ink-750 px-5 py-3">
            {footer}
          </footer>
        ) : null}
      </div>
    </div>,
    document.body,
  );
}
