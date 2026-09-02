/**
 * Runs — every background pipeline execution, with its live log and result.
 *
 * hermes-core turns each long operation (profile import, resume build, job
 * search, tailoring, ATS scoring, sandbox exec) into a Run row plus a stream of
 * RunEvents. This page is the operator's view of that: what ran, how long it
 * took, what it produced, and — while it is still going — what it is doing now.
 *
 * The event stream comes from GET /api/runs/{id}/events, which replays the
 * stored history before switching to live frames, so opening a finished run
 * still shows its whole log.
 *
 * Shared-UI prop contract assumed from '../components':
 *   Badge      {tone?, title?, className?}
 *   Button     {variant?: 'primary'|'ghost'|'danger', size?: 'sm'|'md', className?, ...button}
 *   Card       {title?, subtitle?, actions?, className?, bodyClassName?}
 *   DataTable  {rows, columns:{key,header,render,align?,width?,className?}[], rowKey, onRowClick?, loading?, empty?, dense?}
 *   Drawer     {open, onClose, title, subtitle?, footer?}
 *   EmptyState {title, message?, action?}
 *   ErrorState {title?, message, onRetry?}
 *   LogStream  {lines, connected?, failed?, onReconnect?, className?}
 *   Spinner    {}
 *   StatusDot  {status: string, className?}
 */

import { useCallback, useMemo, useState } from 'react';

import {
  Badge,
  Button,
  Card,
  DataTable,
  Drawer,
  EmptyState,
  ErrorState,
  LogStream,
  Spinner,
  StatusDot,
} from '../components';
import { api, parseJsonish } from '../lib/api';
import { cx, downloadText, fileStamp, fmtClock, fmtDateTime, fmtDuration, fmtNum, fmtRelative } from '../lib/format';
import { useApi, useStream } from '../lib/hooks';
import {
  RUN_KIND_LABELS,
  RUN_KINDS,
  RUN_TERMINAL,
  type Id,
  type Run,
  type RunEvent,
  type RunKind,
  type RunStatus,
} from '../lib/types';

/* -------------------------------------------------------------------------- */
/* Wire-shape adapters                                                        */
/* -------------------------------------------------------------------------- */

/**
 * hermes-core serialises Run through schemas.RunOut, which flattens the JSON
 * columns to `params` / `result` (objects), adds `duration_s`, and includes the
 * stored `events` on the single-run endpoint. lib/types.ts mirrors the DB row
 * instead (`params_json` / `result_json`). Read both.
 */
type RunWire = Run & {
  params?: unknown;
  result?: unknown;
  duration_s?: number | null;
  events?: unknown;
};

function paramsOf(run: Run | null | undefined): Record<string, unknown> | null {
  if (!run) return null;
  return (
    parseJsonish<Record<string, unknown>>(run.params_json) ??
    parseJsonish<Record<string, unknown>>((run as RunWire).params)
  );
}

function resultOf(run: Run | null | undefined): Record<string, unknown> | null {
  if (!run) return null;
  return (
    parseJsonish<Record<string, unknown>>(run.result_json) ??
    parseJsonish<Record<string, unknown>>((run as RunWire).result)
  );
}

function eventsOf(run: Run | null | undefined): RunEvent[] {
  const raw = (run as RunWire | null | undefined)?.events;
  if (!Array.isArray(raw)) return [];
  return raw.filter((entry): entry is RunEvent => !!entry && typeof entry === 'object');
}

function isEmptyPayload(value: Record<string, unknown> | null): boolean {
  return value === null || Object.keys(value).length === 0;
}

function statusOf(run: Run): RunStatus {
  const value = (run.status ?? 'pending') as RunStatus;
  return value === 'pending' || value === 'running' || value === 'done' || value === 'error' ? value : 'pending';
}

function isTerminal(run: Run | null | undefined): boolean {
  if (!run) return false;
  return RUN_TERMINAL.includes(statusOf(run));
}

/** Seconds -> "4.2s" / "1m 42s" / "1h 06m". */
function fmtSeconds(seconds: number): string {
  if (seconds < 60) return `${fmtNum(seconds, 1)}s`;
  const minutes = Math.floor(seconds / 60);
  const rest = Math.round(seconds % 60);
  if (minutes < 60) return `${minutes}m ${rest}s`;
  return `${Math.floor(minutes / 60)}h ${String(minutes % 60).padStart(2, '0')}m`;
}

function durationOf(run: Run): string {
  const seconds = (run as RunWire).duration_s;
  if (typeof seconds === 'number' && Number.isFinite(seconds)) return fmtSeconds(seconds);
  if (!run.started_at) return '—';
  // No duration_s and still running: fmtDuration measures against now.
  return fmtDuration(run.started_at, run.finished_at);
}

const STATUS_TONE: Record<RunStatus, 'neutral' | 'info' | 'good' | 'bad'> = {
  pending: 'neutral',
  running: 'info',
  done: 'good',
  error: 'bad',
};

const STATUS_LABELS: Record<RunStatus, string> = {
  pending: 'Pending',
  running: 'Running',
  done: 'Done',
  error: 'Failed',
};

function kindLabel(kind: string | undefined): string {
  if (!kind) return 'Unknown';
  return RUN_KIND_LABELS[kind as RunKind] ?? kind;
}

const LEVEL_CLASS: Record<string, string> = {
  error: 'text-bad-400',
  warning: 'text-warn-400',
  warn: 'text-warn-400',
  debug: 'text-ink-400',
};

/* -------------------------------------------------------------------------- */
/* Local pieces                                                               */
/* -------------------------------------------------------------------------- */

function JsonBlock({ value, className }: { value: unknown; className?: string }) {
  const text = useMemo(() => {
    try {
      return JSON.stringify(value, null, 2);
    } catch {
      return String(value);
    }
  }, [value]);

  return (
    <pre
      className={cx(
        'overflow-auto rounded-lg border border-ink-700 bg-ink-950 p-3 font-mono text-2xs leading-relaxed text-ink-200',
        className,
      )}
    >
      {text}
    </pre>
  );
}

/** Static fallback log, used when the SSE transport will not connect. */
function StoredEvents({ events }: { events: RunEvent[] }) {
  return (
    <div className="max-h-72 overflow-auto rounded-lg border border-ink-700 bg-ink-950 p-3 font-mono text-2xs leading-relaxed">
      {events.map((event, index) => (
        <div key={event.id ?? index} className="flex gap-2">
          <span className="shrink-0 text-ink-500">{fmtClock(event.ts)}</span>
          <span className={cx('shrink-0 uppercase', LEVEL_CLASS[String(event.level ?? 'info').toLowerCase()] ?? 'text-ink-400')}>
            {String(event.level ?? 'info').slice(0, 4)}
          </span>
          <span className="whitespace-pre-wrap break-words text-ink-200">{event.message ?? ''}</span>
        </div>
      ))}
    </div>
  );
}

/* -------------------------------------------------------------------------- */
/* Page                                                                       */
/* -------------------------------------------------------------------------- */

export default function Runs() {
  const [kindFilter, setKindFilter] = useState<string>('');
  const [statusFilter, setStatusFilter] = useState<RunStatus[]>([]);
  const [openRunId, setOpenRunId] = useState<Id | null>(null);

  const runsQuery = useApi((signal) => api.listRuns(signal), []);
  const runs = useMemo(() => {
    const rows = (runsQuery.data ?? []).slice();
    // Newest first. started_at is an ISO string, so a lexical compare is safe.
    rows.sort((a, b) => String(b.started_at ?? '').localeCompare(String(a.started_at ?? '')));
    return rows;
  }, [runsQuery.data]);

  const anyActive = useMemo(() => runs.some((run) => !isTerminal(run)), [runs]);

  // Poll hard while something is in flight, gently otherwise.
  const reloadRuns = runsQuery.reload;
  useApi(
    async () => {
      reloadRuns();
      return null;
    },
    [reloadRuns],
    { intervalMs: anyActive ? 3_000 : 20_000 },
  );

  const filtered = useMemo(() => {
    const wanted = new Set(statusFilter);
    return runs.filter((run) => {
      if (kindFilter && run.kind !== kindFilter) return false;
      if (wanted.size > 0 && !wanted.has(statusOf(run))) return false;
      return true;
    });
  }, [kindFilter, runs, statusFilter]);

  const listRow = useMemo(
    () => (openRunId === null ? null : runs.find((run) => String(run.id) === String(openRunId)) ?? null),
    [openRunId, runs],
  );

  /* ---- detail ---------------------------------------------------------- */
  const detailQuery = useApi((signal) => api.getRun(openRunId as Id, signal), [openRunId], {
    enabled: openRunId !== null,
    intervalMs: 3_000,
  });

  // useApi keeps the previous payload while a new id is in flight.
  const detail =
    detailQuery.data && String(detailQuery.data.id) === String(openRunId) ? detailQuery.data : null;
  const openRun = detail ?? listRow;

  const stream = useStream(openRunId === null ? null : api.runEventsPath(openRunId), 800);
  const storedEvents = eventsOf(detail);

  const result = resultOf(openRun);
  const params = paramsOf(openRun);

  const toggleStatus = useCallback((status: RunStatus) => {
    setStatusFilter((previous) =>
      previous.includes(status) ? previous.filter((entry) => entry !== status) : [...previous, status],
    );
  }, []);

  const downloadResult = useCallback(() => {
    if (!openRun) return;
    const payload = {
      id: openRun.id,
      kind: openRun.kind,
      status: openRun.status,
      started_at: openRun.started_at,
      finished_at: openRun.finished_at,
      error: openRun.error ?? null,
      params: params ?? {},
      result: result ?? {},
    };
    downloadText(
      `hermes-run-${openRun.id}-${fileStamp()}.json`,
      JSON.stringify(payload, null, 2),
      'application/json;charset=utf-8',
    );
  }, [openRun, params, result]);

  /* ---- table ----------------------------------------------------------- */
  const columns = useMemo(
    () => [
      {
        key: 'kind',
        header: 'Kind',
        render: (run: Run) => (
          <div className="min-w-0">
            <div className="truncate text-sm font-medium text-ink-100">{kindLabel(run.kind)}</div>
            <div className="truncate font-mono text-2xs text-ink-500">{String(run.id)}</div>
          </div>
        ),
      },
      {
        key: 'status',
        header: 'Status',
        width: '132px',
        render: (run: Run) => {
          const status = statusOf(run);
          return (
            <span className="flex items-center gap-2">
              <StatusDot status={status} />
              <span
                className={cx(
                  'text-xs',
                  status === 'error' ? 'text-bad-400' : status === 'done' ? 'text-good-400' : 'text-ink-200',
                )}
              >
                {STATUS_LABELS[status]}
              </span>
            </span>
          );
        },
      },
      {
        key: 'duration',
        header: 'Duration',
        width: '104px',
        align: 'right' as const,
        render: (run: Run) => (
          <span className="font-mono text-2xs tabular-nums text-ink-300">{durationOf(run)}</span>
        ),
      },
      {
        key: 'started',
        header: 'Started',
        width: '160px',
        render: (run: Run) => (
          <span className="text-2xs text-ink-300" title={fmtDateTime(run.started_at)}>
            {run.started_at ? fmtRelative(run.started_at) : 'not started'}
          </span>
        ),
      },
      {
        key: 'outcome',
        header: 'Outcome',
        render: (run: Run) => {
          if (run.error) {
            return (
              <span className="block truncate text-2xs text-bad-400" title={run.error}>
                {run.error}
              </span>
            );
          }
          const payload = resultOf(run);
          if (isEmptyPayload(payload)) {
            return <span className="text-2xs text-ink-500">—</span>;
          }
          const keys = Object.keys(payload as Record<string, unknown>);
          return (
            <span className="block truncate font-mono text-2xs text-ink-400" title={keys.join(', ')}>
              {keys.slice(0, 4).join(', ')}
              {keys.length > 4 ? ` +${keys.length - 4}` : ''}
            </span>
          );
        },
      },
    ],
    [],
  );

  /* ---- render ---------------------------------------------------------- */
  return (
    <div className="flex flex-col gap-4">
      <Card
        title="Runs"
        subtitle={
          anyActive
            ? `${filtered.length} of ${runs.length} shown · refreshing every 3s while work is in flight`
            : `${filtered.length} of ${runs.length} shown`
        }
        actions={
          <div className="flex items-center gap-2">
            {runsQuery.refreshing ? <Spinner /> : null}
            <Button variant="ghost" size="sm" onClick={() => runsQuery.reload()}>
              Refresh
            </Button>
          </div>
        }
      >
        <div className="mb-3 flex flex-wrap items-end gap-4 border-b border-ink-700 pb-3">
          <label className="flex flex-col gap-1">
            <span className="text-2xs font-medium uppercase tracking-wide text-ink-300">Kind</span>
            <select
              className="rounded-md border border-ink-600 bg-ink-850 px-2.5 py-1.5 text-sm text-ink-100 focus:border-brand-400 focus:outline-none"
              value={kindFilter}
              onChange={(event) => setKindFilter(event.target.value)}
            >
              <option value="">All kinds</option>
              {RUN_KINDS.map((kind) => (
                <option key={kind} value={kind}>
                  {RUN_KIND_LABELS[kind]}
                </option>
              ))}
            </select>
          </label>

          <div className="flex flex-wrap items-center gap-1.5 pb-1.5">
            <span className="mr-1 text-2xs font-medium uppercase tracking-wide text-ink-300">Status</span>
            {(['pending', 'running', 'done', 'error'] as RunStatus[]).map((status) => {
              const on = statusFilter.includes(status);
              const count = runs.filter((run) => statusOf(run) === status).length;
              return (
                <button
                  key={status}
                  type="button"
                  aria-pressed={on}
                  onClick={() => toggleStatus(status)}
                  className={cx(
                    'rounded-full border px-2.5 py-1 text-2xs transition-colors',
                    on
                      ? 'border-brand-400 bg-brand-500/20 text-brand-200'
                      : 'border-ink-600 text-ink-300 hover:border-ink-500 hover:text-ink-100',
                  )}
                >
                  {STATUS_LABELS[status]}
                  <span className="ml-1 font-mono tabular-nums text-ink-400">{count}</span>
                </button>
              );
            })}
          </div>

          {kindFilter || statusFilter.length > 0 ? (
            <Button
              variant="ghost"
              size="sm"
              className="mb-0.5"
              onClick={() => {
                setKindFilter('');
                setStatusFilter([]);
              }}
            >
              Clear filters
            </Button>
          ) : null}
        </div>

        {runsQuery.error ? (
          <ErrorState title="Could not load runs" message={runsQuery.error} onRetry={() => runsQuery.reload()} />
        ) : (
          <DataTable
            rows={filtered}
            columns={columns}
            rowKey={(run: Run) => String(run.id)}
            loading={runsQuery.loading}
            onRowClick={(run: Run) => setOpenRunId(run.id)}
            dense
            empty={
              runs.length > 0 ? (
                <EmptyState
                  title="No runs match these filters"
                  message={`${runs.length} runs are hidden by the kind/status filters.`}
                  action={
                    <Button
                      variant="ghost"
                      onClick={() => {
                        setKindFilter('');
                        setStatusFilter([]);
                      }}
                    >
                      Clear filters
                    </Button>
                  }
                />
              ) : (
                <EmptyState
                  title="Nothing has run yet"
                  message="Import your profile, search for jobs or generate a resume — each of those starts a run and streams its progress here."
                />
              )
            }
          />
        )}
      </Card>

      <Drawer
        open={openRun !== null}
        onClose={() => setOpenRunId(null)}
        title={openRun ? kindLabel(openRun.kind) : 'Run detail'}
        subtitle={openRun ? `Run ${openRun.id}` : undefined}
        footer={
          openRun ? (
            <div className="flex items-center justify-between gap-2">
              <span className="text-2xs text-ink-400">
                {isTerminal(openRun)
                  ? `Finished ${fmtRelative(openRun.finished_at ?? openRun.started_at)}`
                  : 'Still running — the log below is live.'}
              </span>
              <Button variant="ghost" size="sm" onClick={downloadResult}>
                Download JSON
              </Button>
            </div>
          ) : null
        }
      >
        {openRun ? (
          <div className="flex flex-col gap-5">
            <div className="flex flex-wrap items-center gap-2">
              <Badge tone={STATUS_TONE[statusOf(openRun)]}>{STATUS_LABELS[statusOf(openRun)]}</Badge>
              <Badge tone="neutral">{kindLabel(openRun.kind)}</Badge>
              <span className="text-2xs text-ink-300" title={fmtDateTime(openRun.started_at)}>
                Started {openRun.started_at ? fmtRelative(openRun.started_at) : '—'}
              </span>
              <span className="font-mono text-2xs tabular-nums text-ink-300">{durationOf(openRun)}</span>
              {detailQuery.refreshing ? <Spinner /> : null}
            </div>

            {openRun.error ? (
              <div className="rounded-lg border border-bad-600/50 bg-bad-600/10 p-3">
                <h4 className="mb-1 text-2xs font-semibold uppercase tracking-wide text-bad-400">Error</h4>
                <p className="whitespace-pre-wrap break-words font-mono text-2xs text-bad-400">{openRun.error}</p>
              </div>
            ) : null}

            <div>
              <h4 className="mb-1.5 text-2xs font-semibold uppercase tracking-wide text-ink-300">Parameters</h4>
              {isEmptyPayload(params) ? (
                <p className="text-xs text-ink-400">This run was started without parameters.</p>
              ) : (
                <JsonBlock value={params} className="max-h-56" />
              )}
            </div>

            <div>
              <div className="mb-1.5 flex items-center justify-between">
                <h4 className="text-2xs font-semibold uppercase tracking-wide text-ink-300">Event log</h4>
                <span className="flex items-center gap-2 text-2xs text-ink-400">
                  <span>{stream.lines.length} lines</span>
                  <button
                    type="button"
                    className="text-brand-300 underline-offset-2 hover:underline"
                    onClick={stream.reconnect}
                  >
                    Reload log
                  </button>
                </span>
              </div>
              {stream.failed && stream.lines.length === 0 && storedEvents.length > 0 ? (
                <div className="flex flex-col gap-1.5">
                  <p className="text-2xs text-warn-400">
                    The live stream would not connect (a proxy may be buffering server-sent events). Showing the{' '}
                    {storedEvents.length} stored events instead.
                  </p>
                  <StoredEvents events={storedEvents} />
                </div>
              ) : (
                <LogStream
                  lines={stream.lines}
                  connected={stream.connected}
                  failed={stream.failed}
                  onReconnect={stream.reconnect}
                  className="h-72"
                />
              )}
            </div>

            <div>
              <h4 className="mb-1.5 text-2xs font-semibold uppercase tracking-wide text-ink-300">Result</h4>
              {isEmptyPayload(result) ? (
                <p className="text-xs text-ink-400">
                  {isTerminal(openRun)
                    ? 'This run finished without a result payload.'
                    : 'No result yet — it is written when the run finishes.'}
                </p>
              ) : (
                <JsonBlock value={result} className="max-h-[28rem]" />
              )}
            </div>
          </div>
        ) : null}
      </Drawer>
    </div>
  );
}
