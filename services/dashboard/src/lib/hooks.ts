/**
 * Small data-fetching hooks shared by the pages.
 *
 * Deliberately dependency-free (no react-query): the whole dashboard talks to a
 * single local FastAPI, and this keeps the bundle tiny and the behaviour obvious.
 */

import {
  type DependencyList,
  useCallback,
  useEffect,
  useMemo,
  useRef,
  useState,
} from 'react';

import { api, decodeStreamLine, errorMessage, sse } from './api';
import { RUN_TERMINAL, type Run, type RunStatus, type Id, type StreamLine } from './types';

export interface UseApiOptions {
  /** Re-fetch on this interval (ms). Omit for a one-shot fetch. */
  intervalMs?: number;
  /** When false, the fetch is skipped entirely (data stays null). */
  enabled?: boolean;
}

export interface UseApiResult<T> {
  data: T | null;
  error: string | null;
  /** True only during the first load; refreshes set `refreshing` instead. */
  loading: boolean;
  refreshing: boolean;
  reload: () => void;
  /** Optimistic local updates (e.g. a PATCHed job row). */
  setData: React.Dispatch<React.SetStateAction<T | null>>;
}

/**
 * Fetch once (and optionally poll). `fetcher` is read from a ref, so it does not
 * need to be memoized - control re-fetching through `deps`.
 */
export function useApi<T>(
  fetcher: (signal: AbortSignal) => Promise<T>,
  deps: DependencyList = [],
  options: UseApiOptions = {},
): UseApiResult<T> {
  const { intervalMs, enabled = true } = options;

  const [data, setData] = useState<T | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState<boolean>(enabled);
  const [refreshing, setRefreshing] = useState(false);
  const [tick, setTick] = useState(0);

  const fetcherRef = useRef(fetcher);
  fetcherRef.current = fetcher;

  // Distinguishes the first load from a poll/refresh of the same query.
  const loadedOnce = useRef(false);

  useEffect(() => {
    if (!enabled) {
      setLoading(false);
      return;
    }
    const controller = new AbortController();
    let cancelled = false;

    if (loadedOnce.current) setRefreshing(true);
    else setLoading(true);

    fetcherRef
      .current(controller.signal)
      .then((result) => {
        if (cancelled) return;
        setData(result);
        setError(null);
      })
      .catch((err: unknown) => {
        if (cancelled) return;
        // A cancelled request is not a user-visible failure.
        if (err instanceof DOMException && err.name === 'AbortError') return;
        setError(errorMessage(err));
      })
      .finally(() => {
        if (cancelled) return;
        loadedOnce.current = true;
        setLoading(false);
        setRefreshing(false);
      });

    return () => {
      cancelled = true;
      controller.abort();
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [...deps, tick, enabled]);

  // Reset the "first load" flag when the query identity changes.
  useEffect(() => {
    loadedOnce.current = false;
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, deps);

  useEffect(() => {
    if (!intervalMs || !enabled) return;
    const handle = window.setInterval(() => setTick((value) => value + 1), intervalMs);
    return () => window.clearInterval(handle);
  }, [intervalMs, enabled]);

  const reload = useCallback(() => setTick((value) => value + 1), []);

  return { data, error, loading, refreshing, reload, setData };
}

/** Run a callback on an interval, with the latest closure. Pass null to pause. */
export function useInterval(callback: () => void, delayMs: number | null): void {
  const callbackRef = useRef(callback);
  callbackRef.current = callback;

  useEffect(() => {
    if (delayMs === null) return;
    const handle = window.setInterval(() => callbackRef.current(), delayMs);
    return () => window.clearInterval(handle);
  }, [delayMs]);
}

export interface UseActionResult<A extends unknown[], R> {
  run: (...args: A) => Promise<R | undefined>;
  busy: boolean;
  error: string | null;
  clearError: () => void;
}

/**
 * Wrap a mutating call (POST/PATCH/DELETE) with busy + error state so buttons
 * can disable themselves and surface a message without bespoke state per page.
 */
export function useAction<A extends unknown[], R>(
  action: (...args: A) => Promise<R>,
  options: { onError?: (message: string) => void } = {},
): UseActionResult<A, R> {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const actionRef = useRef(action);
  actionRef.current = action;
  const onErrorRef = useRef(options.onError);
  onErrorRef.current = options.onError;

  const mounted = useRef(true);
  useEffect(() => {
    mounted.current = true;
    return () => {
      mounted.current = false;
    };
  }, []);

  const run = useCallback(async (...args: A): Promise<R | undefined> => {
    setBusy(true);
    setError(null);
    try {
      return await actionRef.current(...args);
    } catch (err) {
      const message = errorMessage(err);
      if (mounted.current) setError(message);
      onErrorRef.current?.(message);
      return undefined;
    } finally {
      if (mounted.current) setBusy(false);
    }
  }, []);

  const clearError = useCallback(() => setError(null), []);

  return { run, busy, error, clearError };
}

/**
 * Live SSE tail. Keeps at most `limit` lines and reports connection state so the
 * UI can show "connecting / live / disconnected" instead of silently stalling.
 */
export interface UseStreamResult {
  lines: StreamLine[];
  connected: boolean;
  failed: boolean;
  clear: () => void;
  reconnect: () => void;
}

export function useStream(path: string | null, limit = 500): UseStreamResult {
  const [lines, setLines] = useState<StreamLine[]>([]);
  const [connected, setConnected] = useState(false);
  const [failed, setFailed] = useState(false);
  const [epoch, setEpoch] = useState(0);

  useEffect(() => {
    if (!path) {
      setConnected(false);
      setFailed(false);
      return;
    }
    setFailed(false);
    setConnected(false);

    const dispose = sse(
      path,
      (raw) => {
        const line = decodeStreamLine(raw);
        setLines((previous) => {
          const next = previous.length >= limit ? previous.slice(previous.length - limit + 1) : previous.slice();
          next.push(line);
          return next;
        });
      },
      {
        onOpen: () => {
          setConnected(true);
          setFailed(false);
        },
        onError: () => {
          setConnected(false);
          setFailed(true);
        },
      },
    );

    return () => {
      dispose();
      setConnected(false);
    };
  }, [path, limit, epoch]);

  const clear = useCallback(() => setLines([]), []);
  const reconnect = useCallback(() => {
    setLines([]);
    setEpoch((value) => value + 1);
  }, []);

  return { lines, connected, failed, clear, reconnect };
}

/**
 * Poll a single Run until it reaches a terminal status, then fire `onFinish`
 * exactly once. Used to refresh lists after a background pipeline completes.
 */
export function useRunWatch(
  runId: Id | null,
  onFinish?: (run: Run) => void,
  pollMs = 2500,
): { run: Run | null; done: boolean } {
  const [run, setRun] = useState<Run | null>(null);
  const onFinishRef = useRef(onFinish);
  onFinishRef.current = onFinish;
  const firedFor = useRef<string | null>(null);

  useEffect(() => {
    setRun(null);
    firedFor.current = null;
  }, [runId]);

  const status = (run?.status ?? '') as RunStatus;
  const isDone = RUN_TERMINAL.includes(status);

  useEffect(() => {
    if (runId === null || runId === undefined) return;
    if (isDone) return;

    let cancelled = false;
    const controller = new AbortController();

    const poll = async () => {
      try {
        const latest = await api.getRun(runId, controller.signal);
        if (cancelled) return;
        setRun(latest);
      } catch {
        // Transient failures are fine here - the next tick retries.
      }
    };

    void poll();
    const handle = window.setInterval(() => void poll(), pollMs);

    return () => {
      cancelled = true;
      controller.abort();
      window.clearInterval(handle);
    };
  }, [runId, isDone, pollMs]);

  useEffect(() => {
    if (!run || !isDone) return;
    const key = String(run.id);
    if (firedFor.current === key) return;
    firedFor.current = key;
    onFinishRef.current?.(run);
  }, [run, isDone]);

  return { run, done: isDone };
}

/** Persist a small piece of UI state (filters, sort) across reloads. */
export function useLocalState<T>(key: string, initial: T): [T, (value: T) => void] {
  const storageKey = `hermes.${key}`;

  const [value, setValue] = useState<T>(() => {
    try {
      const raw = window.localStorage.getItem(storageKey);
      if (raw === null) return initial;
      return JSON.parse(raw) as T;
    } catch {
      // Private mode / blocked storage - fall back to the default.
      return initial;
    }
  });

  const update = useCallback(
    (next: T) => {
      setValue(next);
      try {
        window.localStorage.setItem(storageKey, JSON.stringify(next));
      } catch {
        // Non-fatal: the value still lives in React state for this session.
      }
    },
    [storageKey],
  );

  return [value, update];
}

/** Debounce a rapidly-changing value (search boxes). */
export function useDebounced<T>(value: T, delayMs = 300): T {
  const [debounced, setDebounced] = useState(value);

  useEffect(() => {
    const handle = window.setTimeout(() => setDebounced(value), delayMs);
    return () => window.clearTimeout(handle);
  }, [value, delayMs]);

  return debounced;
}

/** Stable id generator for accessible label/description wiring. */
export function useDomId(prefix: string): string {
  return useMemo(() => `${prefix}-${Math.random().toString(36).slice(2, 9)}`, [prefix]);
}
