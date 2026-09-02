/**
 * LogStream - the live event log for a run or a container.
 *
 * Presentation only: the EventSource lifecycle lives in `useStream`
 * (lib/hooks.ts), and this component renders whatever that hook reports. That
 * split is deliberate - the hook survives this component unmounting when a
 * drawer closes, so reopening the drawer does not drop the connection.
 *
 * Auto-scroll follows the tail only while the user is already at the bottom. A
 * log that yanks itself back down while someone is reading scrollback is worse
 * than one that does not follow at all, so scrolling up pauses the follow and a
 * button brings it back.
 */

import { useEffect, useRef, useState } from 'react';

import { cx, fmtClock } from '../lib/format';
import type { StreamLine } from '../lib/types';

export interface LogStreamProps {
  lines: readonly StreamLine[];
  connected: boolean;
  failed: boolean;
  onClear?: () => void;
  onReconnect?: () => void;
  emptyText?: string;
  className?: string;
}

const LEVEL_COLOR: Record<string, string> = {
  error: 'text-bad-400',
  warn: 'text-warn-400',
  warning: 'text-warn-400',
  info: 'text-ink-200',
  debug: 'text-ink-400',
};

export function LogStream({
  lines,
  connected,
  failed,
  onClear,
  onReconnect,
  emptyText = 'No output yet.',
  className,
}: LogStreamProps) {
  const scrollRef = useRef<HTMLDivElement | null>(null);
  const [following, setFollowing] = useState(true);

  // Follow the tail on every new line, but only while the user has not scrolled
  // away. `behavior: auto` (not smooth) so a burst of lines does not queue
  // dozens of animations.
  useEffect(() => {
    const el = scrollRef.current;
    if (!el || !following) return;
    el.scrollTop = el.scrollHeight;
  }, [lines.length, following]);

  function handleScroll() {
    const el = scrollRef.current;
    if (!el) return;
    // 24px of slack: "close enough to the bottom" counts as following, so a
    // sub-pixel layout shift does not silently disable auto-scroll.
    const atBottom = el.scrollHeight - el.scrollTop - el.clientHeight < 24;
    setFollowing(atBottom);
  }

  function jumpToLatest() {
    const el = scrollRef.current;
    if (el) el.scrollTop = el.scrollHeight;
    setFollowing(true);
  }

  return (
    <div className={cx('flex min-h-0 flex-col overflow-hidden rounded-md border border-ink-700 bg-ink-950', className)}>
      <div className="flex shrink-0 items-center justify-between gap-2 border-b border-ink-700 bg-ink-875 px-2.5 py-1.5">
        <div className="flex items-center gap-2">
          <span
            className={cx(
              'h-1.5 w-1.5 rounded-full',
              failed ? 'bg-bad-500' : connected ? 'bg-good-500 animate-pulseline' : 'bg-ink-400',
            )}
            aria-hidden="true"
          />
          <span className="text-2xs font-medium uppercase tracking-wide text-ink-300">
            {failed ? 'Stream failed' : connected ? 'Live' : 'Disconnected'}
          </span>
          <span className="text-2xs tabular-nums text-ink-500">
            {lines.length} {lines.length === 1 ? 'line' : 'lines'}
          </span>
        </div>
        <div className="flex items-center gap-1.5">
          {!following && lines.length > 0 && (
            <button
              type="button"
              onClick={jumpToLatest}
              className="rounded px-1.5 py-0.5 text-2xs text-brand-300 transition-colors hover:bg-ink-800 hover:text-brand-200 focus:outline-none focus-visible:ring-2 focus-visible:ring-brand-400"
            >
              Jump to latest
            </button>
          )}
          {(failed || !connected) && onReconnect && (
            <button
              type="button"
              onClick={onReconnect}
              className="rounded px-1.5 py-0.5 text-2xs text-ink-200 transition-colors hover:bg-ink-800 hover:text-ink-100 focus:outline-none focus-visible:ring-2 focus-visible:ring-brand-400"
            >
              Reconnect
            </button>
          )}
          {onClear && lines.length > 0 && (
            <button
              type="button"
              onClick={onClear}
              className="rounded px-1.5 py-0.5 text-2xs text-ink-300 transition-colors hover:bg-ink-800 hover:text-ink-100 focus:outline-none focus-visible:ring-2 focus-visible:ring-brand-400"
            >
              Clear
            </button>
          )}
        </div>
      </div>

      <div
        ref={scrollRef}
        onScroll={handleScroll}
        className="min-h-0 flex-1 overflow-y-auto overflow-x-auto px-2.5 py-2 font-mono text-2xs leading-relaxed"
        role="log"
        aria-live="polite"
        aria-atomic="false"
      >
        {lines.length === 0 ? (
          <p className="py-6 text-center font-sans text-xs text-ink-400">
            {failed ? 'The event stream could not be opened.' : emptyText}
          </p>
        ) : (
          lines.map((line) => (
            <div key={line.key} className="flex gap-2 whitespace-pre-wrap break-words">
              <span className="shrink-0 select-none tabular-nums text-ink-500">
                {fmtClock(line.ts)}
              </span>
              <span className={cx('min-w-0', LEVEL_COLOR[line.level] ?? 'text-ink-200')}>
                {line.message}
              </span>
            </div>
          ))
        )}
      </div>
    </div>
  );
}

export default LogStream;
