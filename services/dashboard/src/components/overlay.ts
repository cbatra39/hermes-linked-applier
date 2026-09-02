/**
 * Accessibility plumbing shared by Modal and Drawer.
 *
 * Implemented by hand (no focus-trap / react-modal dependency) because
 * package.json is frozen. Three behaviours, each in its own hook so either
 * overlay can opt out:
 *   - lock body scroll while something is over the page (ref-counted, so a
 *     Drawer opened from inside a Modal does not unlock on the inner close);
 *   - trap Tab inside the panel and restore focus to the trigger on close;
 *   - close on Escape.
 */

import { useEffect, useRef, type RefObject } from 'react';

/** Everything the browser will focus, minus anything explicitly removed. */
export const FOCUSABLE_SELECTOR = [
  'a[href]',
  'button:not([disabled])',
  'input:not([disabled]):not([type="hidden"])',
  'select:not([disabled])',
  'textarea:not([disabled])',
  '[tabindex]:not([tabindex="-1"])',
  '[contenteditable="true"]',
].join(',');

function focusableWithin(root: HTMLElement): HTMLElement[] {
  return Array.from(root.querySelectorAll<HTMLElement>(FOCUSABLE_SELECTOR)).filter((element) => {
    if (element.hasAttribute('disabled')) return false;
    if (element.getAttribute('aria-hidden') === 'true') return false;
    // offsetParent is null for display:none subtrees; <dialog>-less overlays are
    // always positioned, so this is a reliable visibility check here.
    return element.offsetParent !== null || element === document.activeElement;
  });
}

let scrollLockCount = 0;
let previousOverflow = '';
let previousPaddingRight = '';

/** Prevent the page behind an overlay from scrolling. Ref-counted. */
export function useBodyScrollLock(active: boolean): void {
  useEffect(() => {
    if (!active) return;

    if (scrollLockCount === 0) {
      const { body } = document;
      previousOverflow = body.style.overflow;
      previousPaddingRight = body.style.paddingRight;
      // Compensate for the scrollbar we are about to remove, or the whole page
      // shifts sideways the moment a modal opens.
      const gap = window.innerWidth - document.documentElement.clientWidth;
      if (gap > 0) body.style.paddingRight = `${gap}px`;
      body.style.overflow = 'hidden';
    }
    scrollLockCount += 1;

    return () => {
      scrollLockCount = Math.max(0, scrollLockCount - 1);
      if (scrollLockCount === 0) {
        document.body.style.overflow = previousOverflow;
        document.body.style.paddingRight = previousPaddingRight;
      }
    };
  }, [active]);
}

/** Call `handler` when Escape is pressed while `active`. */
export function useEscapeKey(active: boolean, handler: () => void): void {
  const handlerRef = useRef(handler);
  handlerRef.current = handler;

  useEffect(() => {
    if (!active) return;
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key !== 'Escape') return;
      event.stopPropagation();
      handlerRef.current();
    };
    document.addEventListener('keydown', onKeyDown, true);
    return () => document.removeEventListener('keydown', onKeyDown, true);
  }, [active]);
}

/**
 * Keep keyboard focus inside `containerRef` while `active`, then hand it back to
 * whatever was focused before the overlay opened.
 */
export function useFocusTrap(
  active: boolean,
  containerRef: RefObject<HTMLElement>,
  initialFocusRef?: RefObject<HTMLElement>,
): void {
  useEffect(() => {
    if (!active) return;
    const container = containerRef.current;
    if (!container) return;

    const restoreTo = document.activeElement instanceof HTMLElement ? document.activeElement : null;

    // Give React a frame to paint the panel's children before hunting for focus.
    const focusFrame = window.requestAnimationFrame(() => {
      const explicit = initialFocusRef?.current;
      if (explicit) {
        explicit.focus();
        return;
      }
      const [first] = focusableWithin(container);
      (first ?? container).focus();
    });

    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key !== 'Tab') return;
      const items = focusableWithin(container);
      if (!items.length) {
        // Nothing to tab to: keep focus pinned on the panel itself.
        event.preventDefault();
        container.focus();
        return;
      }
      const first = items[0];
      const last = items[items.length - 1];
      const current = document.activeElement;

      if (event.shiftKey && (current === first || current === container || !container.contains(current))) {
        event.preventDefault();
        last.focus();
      } else if (!event.shiftKey && current === last) {
        event.preventDefault();
        first.focus();
      }
    };

    container.addEventListener('keydown', onKeyDown);

    return () => {
      window.cancelAnimationFrame(focusFrame);
      container.removeEventListener('keydown', onKeyDown);
      // Only steal focus back if it is still inside the closing overlay.
      if (restoreTo && document.body.contains(restoreTo)) {
        const active_ = document.activeElement;
        if (!active_ || active_ === document.body || container.contains(active_)) {
          restoreTo.focus();
        }
      }
    };
  }, [active, containerRef, initialFocusRef]);
}
