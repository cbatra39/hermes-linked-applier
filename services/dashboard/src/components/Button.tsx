/**
 * Buttons.
 *
 * Two components share one class builder so a real download link (`<a href>`,
 * e.g. api.resumeDownloadUrl()) is visually identical to a `<button>` without
 * pretending to be one.
 */

import { forwardRef, type AnchorHTMLAttributes, type ButtonHTMLAttributes, type ReactNode } from 'react';

import { cx } from '../lib/format';
import { Spinner } from './Spinner';

export type ButtonVariant = 'primary' | 'secondary' | 'ghost' | 'danger' | 'subtle';
export type ButtonSize = 'xs' | 'sm' | 'md';

const VARIANT: Record<ButtonVariant, string> = {
  primary:
    'bg-brand-500 text-ink-950 font-semibold border border-brand-400 hover:bg-brand-400 active:bg-brand-600 shadow-sm',
  secondary:
    'bg-ink-800 text-ink-100 border border-ink-600 hover:bg-ink-750 hover:border-ink-500 active:bg-ink-800',
  ghost: 'bg-transparent text-ink-200 border border-transparent hover:bg-ink-800 hover:text-ink-100',
  danger: 'bg-bad-600 text-white font-semibold border border-bad-500 hover:bg-bad-500 active:bg-bad-600',
  subtle: 'bg-ink-850 text-ink-200 border border-ink-750 hover:bg-ink-800 hover:text-ink-100',
};

const SIZE: Record<ButtonSize, string> = {
  xs: 'h-7 gap-1.5 px-2 text-2xs',
  sm: 'h-8 gap-1.5 px-2.5 text-xs',
  md: 'h-9 gap-2 px-3.5 text-sm',
};

/** Shared class string for a button-shaped control. */
export function buttonClasses(
  variant: ButtonVariant = 'secondary',
  size: ButtonSize = 'md',
  block = false,
  extra?: string,
): string {
  return cx(
    'focus-ring inline-flex select-none items-center justify-center whitespace-nowrap rounded-md',
    'transition-colors duration-100',
    'disabled:pointer-events-none disabled:opacity-50',
    'aria-disabled:pointer-events-none aria-disabled:opacity-50',
    'no-underline hover:no-underline',
    SIZE[size],
    VARIANT[variant],
    block && 'w-full',
    extra,
  );
}

export interface ButtonProps extends ButtonHTMLAttributes<HTMLButtonElement> {
  variant?: ButtonVariant;
  size?: ButtonSize;
  /** Swaps the leading icon for a spinner and disables the button. */
  loading?: boolean;
  /** Alias of `loading`. Both spellings appear across the pages. */
  busy?: boolean;
  icon?: ReactNode;
  iconRight?: ReactNode;
  block?: boolean;
}

export const Button = forwardRef<HTMLButtonElement, ButtonProps>(function Button(
  { variant = 'secondary', size = 'md', loading = false, busy = false, icon, iconRight, block, className, children, disabled, type, ...rest },
  ref,
) {
  const iconPx = size === 'md' ? 16 : 14;
  const pending = loading || busy;
  return (
    <button
      ref={ref}
      // Default to "button": an unqualified <button> inside a <form> submits it.
      type={type ?? 'button'}
      disabled={disabled || pending}
      aria-busy={pending || undefined}
      className={buttonClasses(variant, size, block, className)}
      {...rest}
    >
      {pending ? <Spinner size={iconPx} label="" /> : icon}
      {children ? <span className="truncate">{children}</span> : null}
      {!pending && iconRight ? iconRight : null}
    </button>
  );
});

export interface AnchorButtonProps extends AnchorHTMLAttributes<HTMLAnchorElement> {
  variant?: ButtonVariant;
  size?: ButtonSize;
  icon?: ReactNode;
  iconRight?: ReactNode;
  block?: boolean;
  /** Renders as a non-navigating, dimmed control (no href emitted). */
  disabled?: boolean;
}

/** A link that looks like a button. Use for downloads and external apply links. */
export const AnchorButton = forwardRef<HTMLAnchorElement, AnchorButtonProps>(function AnchorButton(
  { variant = 'secondary', size = 'md', icon, iconRight, block, className, children, disabled, href, target, rel, ...rest },
  ref,
) {
  const external = target === '_blank';
  return (
    <a
      ref={ref}
      href={disabled ? undefined : href}
      target={disabled ? undefined : target}
      // Never hand an external tab a live window.opener.
      rel={rel ?? (external ? 'noopener noreferrer' : undefined)}
      aria-disabled={disabled || undefined}
      tabIndex={disabled ? -1 : undefined}
      className={buttonClasses(variant, size, block, className)}
      {...rest}
    >
      {icon}
      {children ? <span className="truncate">{children}</span> : null}
      {iconRight}
    </a>
  );
});

/** Square icon-only button. `label` is mandatory - it becomes the accessible name. */
export interface IconButtonProps extends Omit<ButtonProps, 'children' | 'iconRight' | 'block'> {
  label: string;
}

export const IconButton = forwardRef<HTMLButtonElement, IconButtonProps>(function IconButton(
  { label, size = 'sm', variant = 'ghost', className, icon, loading, disabled, type, ...rest },
  ref,
) {
  const box = size === 'md' ? 'h-9 w-9' : size === 'sm' ? 'h-8 w-8' : 'h-7 w-7';
  return (
    <button
      ref={ref}
      type={type ?? 'button'}
      title={label}
      aria-label={label}
      disabled={disabled || loading}
      aria-busy={loading || undefined}
      className={buttonClasses(variant, size, false, cx('!px-0', box, className))}
      {...rest}
    >
      {loading ? <Spinner size={size === 'md' ? 16 : 14} label="" /> : icon}
    </button>
  );
});
