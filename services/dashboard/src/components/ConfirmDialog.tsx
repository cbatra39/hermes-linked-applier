/**
 * "Are you sure?" dialog.
 *
 * `requireText` adds a type-to-confirm gate for genuinely destructive calls -
 * DELETE /api/containers/{id} can remove a container Hermes itself depends on,
 * so the operator types its name first.
 */

import { useEffect, useRef, useState, type ReactNode } from 'react';

import { cx } from '../lib/format';
import { Button } from './Button';
import { ErrorState } from './ErrorState';
import { Modal } from './Modal';

export interface ConfirmDialogProps {
  open: boolean;
  title: string;
  /** What is about to happen, and what it cannot undo. */
  message: ReactNode;
  confirmLabel?: string;
  cancelLabel?: string;
  /** `danger` paints the confirm button red. Default for destructive actions. */
  tone?: 'danger' | 'primary';
  /** Disables both buttons and spins the confirm button. */
  busy?: boolean;
  /** Failure from the last attempt; the dialog stays open so it can be read. */
  error?: string | null;
  /** Exact string the user must type before Confirm enables. */
  requireText?: string;
  onConfirm: () => void;
  onCancel: () => void;
}

export function ConfirmDialog({
  open,
  title,
  message,
  confirmLabel = 'Confirm',
  cancelLabel = 'Cancel',
  tone = 'danger',
  busy = false,
  error = null,
  requireText,
  onConfirm,
  onCancel,
}: ConfirmDialogProps) {
  const [typed, setTyped] = useState('');
  const inputRef = useRef<HTMLInputElement>(null);
  const cancelRef = useRef<HTMLButtonElement>(null);

  // Never carry a previous confirmation's typing into the next dialog.
  useEffect(() => {
    if (open) setTyped('');
  }, [open]);

  const gated = Boolean(requireText);
  const satisfied = !gated || typed.trim() === requireText;

  return (
    <Modal
      open={open}
      onClose={busy ? () => undefined : onCancel}
      title={title}
      size="sm"
      closeOnBackdrop={!busy}
      hideCloseButton={busy}
      // Focus lands on Cancel (or the gate input): never pre-arm a destructive button.
      initialFocusRef={gated ? inputRef : cancelRef}
      footer={
        <>
          <Button ref={cancelRef} variant="ghost" onClick={onCancel} disabled={busy}>
            {cancelLabel}
          </Button>
          <Button
            variant={tone === 'danger' ? 'danger' : 'primary'}
            onClick={onConfirm}
            loading={busy}
            disabled={!satisfied}
          >
            {confirmLabel}
          </Button>
        </>
      }
    >
      <div className="space-y-3 text-sm leading-relaxed text-ink-200">
        <div>{message}</div>

        {gated ? (
          <label className="block">
            <span className="field-label">
              Type <span className="font-mono normal-case text-ink-100">{requireText}</span> to confirm
            </span>
            <input
              ref={inputRef}
              type="text"
              value={typed}
              onChange={(event) => setTyped(event.target.value)}
              onKeyDown={(event) => {
                if (event.key === 'Enter' && satisfied && !busy) onConfirm();
              }}
              autoComplete="off"
              spellCheck={false}
              disabled={busy}
              className={cx('field font-mono', typed && !satisfied && 'border-bad-600')}
              placeholder={requireText}
            />
          </label>
        ) : null}

        {error ? <ErrorState message={error} title="That did not work" compact /> : null}
      </div>
    </Modal>
  );
}
