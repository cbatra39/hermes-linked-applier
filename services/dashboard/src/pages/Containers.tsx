/**
 * Containers — the container/sandbox control panel.
 *
 * Three things live here:
 *
 *   1. A polled table of the Docker containers hermes-core can see, with
 *      per-row start / stop / restart / remove and live CPU + memory.
 *   2. A log Drawer streaming GET /api/containers/{id}/logs over SSE.
 *   3. A "Sandbox exec" panel that POSTs python to /api/sandbox/exec, which runs
 *      it inside a throwaway hardened container (no network, read-only rootfs,
 *      every capability dropped) and returns stdout/stderr/exit code.
 *
 * Docker is optional: hermes-core answers 503 when /var/run/docker.sock is not
 * mounted, and this page must explain that rather than looking broken.
 */

import { useCallback, useMemo, useState } from 'react';

import {
  Badge,
  Button,
  Card,
  ConfirmDialog,
  DataTable,
  Drawer,
  EmptyState,
  ErrorState,
  LogStream,
  Spinner,
  StatusDot,
} from '../components';
import { ApiError, api, errorMessage } from '../lib/api';
import { cx, fmtBytes, fmtNum, fmtPorts, fmtRelative, shortId, truncate } from '../lib/format';
import { useAction, useApi, useStream } from '../lib/hooks';
import type { ContainerInfo, ContainerStats, Run, SandboxExecResponse } from '../lib/types';

/* -------------------------------------------------------------------------- */
/* Constants                                                                   */
/* -------------------------------------------------------------------------- */

const LIST_POLL_MS = 6_000;
const STATS_POLL_MS = 8_000;
const LOG_TAIL = 400;

const SANDBOX_SAMPLE = `import os
import platform
import sys

print("python", sys.version.split()[0], "on", platform.machine())
print("uid:", os.getuid(), "cwd:", os.getcwd())
print("network:", "disabled" if not os.environ.get("HTTP_PROXY") else "proxied")

work = "/work"
if os.path.isdir(work):
    print("workspace files:", sorted(os.listdir(work)) or "(empty)")
else:
    print("no", work, "mount")
`;

/** Containers whose death takes the dashboard or the API with it. */
const CRITICAL_ROLES = new Set(['core', 'dashboard']);
const CRITICAL_NAME_HINTS = ['hermes-core', 'hermes-dashboard'];

type Tone = 'good' | 'warn' | 'bad' | 'neutral' | 'info';
type RowAction = 'start' | 'stop' | 'restart' | 'remove';

/* -------------------------------------------------------------------------- */
/* Helpers                                                                     */
/* -------------------------------------------------------------------------- */

/** Read an undeclared key off a loosely-typed payload without lying with a cast. */
function pick<T>(source: unknown, key: string): T | undefined {
  if (source && typeof source === 'object' && key in source) {
    return (source as Record<string, unknown>)[key] as T;
  }
  return undefined;
}

function containerState(container: ContainerInfo): string {
  return String(container.state ?? container.status ?? '').toLowerCase();
}

function isRunning(container: ContainerInfo): boolean {
  const state = containerState(container);
  return state.startsWith('running') || state.startsWith('up') || state === 'restarting';
}

function stateTone(container: ContainerInfo): Tone {
  const state = containerState(container);
  if (state.startsWith('running') || state.startsWith('up')) return 'good';
  if (state === 'restarting' || state.startsWith('created')) return 'info';
  if (state.startsWith('paused')) return 'warn';
  if (state.startsWith('exited') || state.startsWith('dead')) return 'bad';
  return 'neutral';
}

function displayName(container: ContainerInfo): string {
  const name = (container.name ?? '').replace(/^\//, '').trim();
  return name || shortId(container.id);
}

function isCritical(container: ContainerInfo): boolean {
  const role = String(pick<string>(container, 'hermes_role') ?? '').toLowerCase();
  if (CRITICAL_ROLES.has(role)) return true;
  const name = displayName(container).toLowerCase();
  return CRITICAL_NAME_HINTS.some((hint) => name.includes(hint));
}

/**
 * Render a port map.
 *
 * `ContainerOut.ports` (schemas.py) is a list of snake_case objects
 * `{ip, private_port, public_port, type}`, which `fmtPorts()` in lib/format.ts
 * does NOT understand — it only handles Docker's PascalCase SDK shape and the
 * dict-of-bindings shape. Handle snake_case here first, then delegate.
 */
function renderPorts(ports: unknown): string {
  if (Array.isArray(ports)) {
    const parts = ports
      .map((entry) => {
        if (!entry || typeof entry !== 'object') return '';
        const privatePort = pick<number>(entry, 'private_port');
        if (privatePort === undefined || privatePort === null) return '';
        const publicPort = pick<number>(entry, 'public_port');
        const proto = pick<string>(entry, 'type') ?? 'tcp';
        const ip = pick<string>(entry, 'ip') ?? '0.0.0.0';
        return publicPort ? `${ip}:${publicPort}->${privatePort}/${proto}` : `${privatePort}/${proto}`;
      })
      .filter(Boolean);
    if (parts.length) return parts.join(', ');
  }
  return fmtPorts(ports);
}

interface ContainersView {
  items: ContainerInfo[];
  dockerOk: boolean;
  detail: string;
}

interface ExecView {
  exitCode: number | null;
  stdout: string;
  stderr: string;
  artifacts: string[];
  containerId: string;
  runId: string | null;
  runError: string | null;
}

/** `POST /api/sandbox/exec` answers `{run, result}`; older builds merged them. */
function normalizeExec(payload: SandboxExecResponse | undefined | null): ExecView | null {
  if (!payload) return null;
  const result = payload.result ?? null;
  const run: Run | null = payload.run ?? null;
  const topLevelId = pick<string | number>(payload, 'id');

  return {
    exitCode: result?.exit_code ?? payload.exit_code ?? null,
    stdout: String(result?.stdout ?? payload.stdout ?? ''),
    stderr: String(result?.stderr ?? payload.stderr ?? ''),
    artifacts: result?.artifacts ?? payload.artifacts ?? [],
    containerId: String(result?.container_id ?? payload.container_id ?? ''),
    runId: run?.id !== undefined ? String(run.id) : topLevelId !== undefined ? String(topLevelId) : null,
    runError: run?.error ?? pick<string>(payload, 'error') ?? null,
  };
}

/* -------------------------------------------------------------------------- */
/* Docker-unavailable banner                                                   */
/* -------------------------------------------------------------------------- */

function DockerBanner({ detail }: { detail: string }) {
  return (
    <div
      role="alert"
      className="rounded-lg border border-warn-600/50 bg-warn-500/10 p-4 text-sm text-ink-100"
    >
      <p className="font-semibold text-warn-400">Docker is not available to hermes-core</p>
      <p className="mt-1.5 leading-relaxed text-ink-200">
        Container control and the sandbox both talk to the Docker daemon through its socket. Mount it
        into the <code className="font-mono text-brand-300">hermes-core</code> service:
      </p>
      <pre className="mt-2 overflow-x-auto rounded-md border border-ink-700 bg-ink-950 p-3 font-mono text-2xs leading-relaxed text-ink-200">
        {`services:
  hermes-core:
    volumes:
      - /var/run/docker.sock:/var/run/docker.sock:ro`}
      </pre>
      <p className="mt-2 text-xs leading-relaxed text-ink-300">
        On Docker Desktop for Windows the socket is proxied automatically once the volume is present.
        After editing <code className="font-mono">docker-compose.yml</code>, run{' '}
        <code className="font-mono text-brand-300">make restart</code>.
      </p>
      {detail && (
        <p className="mt-2 border-t border-warn-600/30 pt-2 font-mono text-2xs text-ink-300">
          {truncate(detail, 300)}
        </p>
      )}
    </div>
  );
}

/* -------------------------------------------------------------------------- */
/* Sandbox exec panel                                                         */
/* -------------------------------------------------------------------------- */

function SandboxPanel({ dockerOk }: { dockerOk: boolean }) {
  const [code, setCode] = useState<string>(SANDBOX_SAMPLE);
  const [view, setView] = useState<ExecView | null>(null);

  const exec = useAction(async (source: string) => {
    const response = await api.sandboxExec(source);
    setView(normalizeExec(response));
    return response;
  });

  const submit = () => {
    const source = code.trim();
    if (!source) return;
    setView(null);
    void exec.run(source);
  };

  const exitTone: Tone = view === null ? 'neutral' : view.exitCode === 0 ? 'good' : 'bad';

  return (
    <Card
      title="Sandbox exec"
      subtitle="Ephemeral, locked-down container — nothing here can reach your host"
      actions={
        <Badge tone="info" title="Every exec creates and destroys its own container">
          throwaway
        </Badge>
      }
    >
      <ul className="mb-3 grid grid-cols-1 gap-1 text-2xs leading-relaxed text-ink-400 sm:grid-cols-2">
        <li>Networking disabled (no internet, no access to the other services)</li>
        <li>Read-only root filesystem, 64 MB writable /tmp only</li>
        <li>Runs as uid 1000, every Linux capability dropped, no-new-privileges</li>
        <li>Memory, CPU, PID and wall-clock limits from the sandbox settings</li>
      </ul>

      <label htmlFor="sandbox-code" className="mb-1.5 block text-xs font-medium text-ink-300">
        Python source
      </label>
      <textarea
        id="sandbox-code"
        value={code}
        onChange={(event) => setCode(event.target.value)}
        spellCheck={false}
        rows={10}
        className="block w-full resize-y rounded-md border border-ink-700 bg-ink-950 p-3 font-mono text-xs leading-relaxed text-ink-100 placeholder:text-ink-500 focus:border-brand-600 focus:outline-none focus:ring-1 focus:ring-brand-600"
        placeholder="print('hello from the sandbox')"
      />

      <div className="mt-3 flex flex-wrap items-center gap-2">
        <Button onClick={submit} busy={exec.busy} disabled={exec.busy || !code.trim() || !dockerOk}>
          {exec.busy ? 'Running…' : 'Run in sandbox'}
        </Button>
        <Button
          variant="ghost"
          onClick={() => {
            setView(null);
            exec.clearError();
          }}
          disabled={exec.busy || (view === null && !exec.error)}
        >
          Clear result
        </Button>
        <Button variant="ghost" onClick={() => setCode(SANDBOX_SAMPLE)} disabled={exec.busy}>
          Reset sample
        </Button>
        {!dockerOk && (
          <span className="text-2xs text-warn-400">Needs Docker — see the banner above.</span>
        )}
      </div>

      {exec.error && (
        <div className="mt-3">
          <ErrorState title="Sandbox exec failed" message={exec.error} onRetry={submit} />
        </div>
      )}

      {view && (
        <div className="mt-4 space-y-3 border-t border-ink-800 pt-4">
          <div className="flex flex-wrap items-center gap-2">
            <Badge tone={exitTone}>exit {view.exitCode ?? '?'}</Badge>
            {view.containerId && (
              <span className="font-mono text-2xs text-ink-400">
                container {shortId(view.containerId)}
              </span>
            )}
            {view.runId && <span className="font-mono text-2xs text-ink-400">run #{view.runId}</span>}
            {view.artifacts.length > 0 && (
              <Badge tone="neutral">
                {view.artifacts.length} artifact{view.artifacts.length === 1 ? '' : 's'}
              </Badge>
            )}
          </div>

          {view.runError && (
            <p className="rounded-md border border-bad-600/40 bg-bad-500/10 p-2 text-xs text-bad-400">
              {view.runError}
            </p>
          )}

          <StreamBlock label="stdout" text={view.stdout} tone="neutral" />
          <StreamBlock label="stderr" text={view.stderr} tone="bad" />

          {view.artifacts.length > 0 && (
            <div>
              <p className="mb-1 text-2xs font-semibold uppercase tracking-wider text-ink-400">
                artifacts (left in the run workspace on disk)
              </p>
              <ul className="space-y-0.5 font-mono text-2xs text-ink-200">
                {view.artifacts.map((path) => (
                  <li key={path} className="truncate" title={path}>
                    {path}
                  </li>
                ))}
              </ul>
            </div>
          )}
        </div>
      )}
    </Card>
  );
}

function StreamBlock({ label, text, tone }: { label: string; text: string; tone: 'neutral' | 'bad' }) {
  const empty = !text.trim();
  return (
    <div>
      <p className="mb-1 text-2xs font-semibold uppercase tracking-wider text-ink-400">{label}</p>
      <pre
        className={cx(
          'max-h-64 overflow-auto whitespace-pre-wrap break-words rounded-md border p-3 font-mono text-2xs leading-relaxed',
          empty && 'border-ink-800 bg-ink-900 text-ink-400',
          !empty && tone === 'bad' && 'border-bad-600/40 bg-bad-500/5 text-bad-400',
          !empty && tone === 'neutral' && 'border-ink-700 bg-ink-950 text-ink-100',
        )}
      >
        {empty ? '(empty)' : text}
      </pre>
    </div>
  );
}

/* -------------------------------------------------------------------------- */
/* Page                                                                        */
/* -------------------------------------------------------------------------- */

export default function Containers() {
  const [logTarget, setLogTarget] = useState<ContainerInfo | null>(null);
  const [removeTarget, setRemoveTarget] = useState<ContainerInfo | null>(null);
  const [pending, setPending] = useState<{ id: string; action: RowAction } | null>(null);
  const [actionError, setActionError] = useState<string | null>(null);
  const [actionNote, setActionNote] = useState<string | null>(null);

  /* ---- container list (503 = Docker not wired up, not a crash) ---------- */
  const list = useApi<ContainersView>(
    async (signal) => {
      try {
        const items = await api.listContainers(signal);
        return { items, dockerOk: true, detail: '' };
      } catch (err) {
        // hermes-core answers 503 when the daemon is unreachable; that is a
        // configuration state with its own UI, not a failed page load.
        if (err instanceof ApiError && (err.status === 503 || err.status === 501)) {
          return { items: [], dockerOk: false, detail: errorMessage(err) };
        }
        throw err;
      }
    },
    [],
    { intervalMs: LIST_POLL_MS },
  );

  const containers = useMemo(() => {
    const rows = list.data?.items ?? [];
    return rows.slice().sort((a, b) => {
      // Running first, then alphabetical - the thing you want is at the top.
      const delta = Number(isRunning(b)) - Number(isRunning(a));
      if (delta !== 0) return delta;
      return displayName(a).localeCompare(displayName(b));
    });
  }, [list.data]);

  // Only a real 503 from hermes-core means "Docker is not wired up"; a network
  // failure or a 500 is a different problem and gets an ErrorState instead.
  const dockerOk = list.data?.dockerOk ?? true;

  /* ---- live stats for the running containers ---------------------------- */
  const runningKey = useMemo(
    () =>
      containers
        .filter(isRunning)
        .map((container) => container.id)
        .sort()
        .join(','),
    [containers],
  );

  const stats = useApi<Record<string, ContainerStats>>(
    async (signal) => {
      const ids = runningKey ? runningKey.split(',') : [];
      const pairs = await Promise.all(
        ids.map(async (id) => {
          try {
            return [id, await api.containerStats(id, signal)] as const;
          } catch {
            // A container can vanish between the list and the stats call.
            return [id, null] as const;
          }
        }),
      );
      const out: Record<string, ContainerStats> = {};
      for (const [id, value] of pairs) if (value) out[id] = value;
      return out;
    },
    [runningKey],
    { intervalMs: STATS_POLL_MS, enabled: runningKey.length > 0 },
  );

  const statsById = stats.data ?? {};

  // `useApi` returns a fresh result object each render but `reload` is stable,
  // so depend on the callbacks - otherwise the columns memo below never hits.
  const reloadList = list.reload;
  const reloadStats = stats.reload;

  /* ---- logs ------------------------------------------------------------- */
  const logPath = logTarget ? api.containerLogsPath(logTarget.id, LOG_TAIL) : null;
  const logStream = useStream(logPath, 1000);

  /* ---- row actions ------------------------------------------------------ */
  const runRowAction = useCallback(
    async (container: ContainerInfo, action: RowAction, force = false) => {
      setPending({ id: container.id, action });
      setActionError(null);
      setActionNote(null);
      try {
        if (action === 'remove') {
          await api.removeContainer(container.id, force);
          setActionNote(`Removed ${displayName(container)}.`);
        } else {
          await api.containerAction(container.id, action);
          setActionNote(`${action === 'start' ? 'Started' : action === 'stop' ? 'Stopped' : 'Restarted'} ${displayName(container)}.`);
        }
        reloadList();
        reloadStats();
      } catch (err) {
        setActionError(`${action} ${displayName(container)}: ${errorMessage(err)}`);
      } finally {
        setPending(null);
      }
    },
    [reloadList, reloadStats],
  );

  const isPending = (container: ContainerInfo, action: RowAction) =>
    pending?.id === container.id && pending.action === action;
  const anyPending = (container: ContainerInfo) => pending?.id === container.id;

  const columns = useMemo(
    () => [
      {
        key: 'name',
        header: 'Container',
        render: (row: ContainerInfo) => (
          <div className="min-w-0">
            <div className="flex items-center gap-2">
              <StatusDot tone={stateTone(row)} pulse={containerState(row) === 'restarting'} />
              <span className="truncate font-medium text-ink-100" title={displayName(row)}>
                {displayName(row)}
              </span>
              {isCritical(row) && (
                <Badge tone="warn" title="Stopping this breaks the dashboard or the API">
                  core
                </Badge>
              )}
            </div>
            <p className="mt-0.5 truncate font-mono text-2xs text-ink-400" title={row.id}>
              {shortId(row.id)}
              {row.created ? ` · ${fmtRelative(row.created)}` : ''}
            </p>
          </div>
        ),
      },
      {
        key: 'image',
        header: 'Image',
        render: (row: ContainerInfo) => (
          <span className="block max-w-[18rem] truncate font-mono text-2xs text-ink-300" title={row.image ?? ''}>
            {row.image || '-'}
          </span>
        ),
      },
      {
        key: 'state',
        header: 'State',
        render: (row: ContainerInfo) => (
          <div className="min-w-0">
            <Badge tone={stateTone(row)}>{row.state || 'unknown'}</Badge>
            <p className="mt-0.5 truncate text-2xs text-ink-400" title={row.status ?? ''}>
              {row.status || ''}
            </p>
          </div>
        ),
      },
      {
        key: 'ports',
        header: 'Ports',
        render: (row: ContainerInfo) => {
          const text = renderPorts(row.ports);
          return (
            <span className="block max-w-[16rem] truncate font-mono text-2xs text-ink-300" title={text}>
              {text || '-'}
            </span>
          );
        },
      },
      {
        key: 'cpu',
        header: 'CPU',
        align: 'right' as const,
        render: (row: ContainerInfo) => {
          if (!isRunning(row)) return <span className="text-2xs text-ink-400">-</span>;
          const value = statsById[row.id]?.cpu_percent;
          if (value === undefined || value === null) {
            return <span className="text-2xs text-ink-400">{stats.loading ? '…' : '-'}</span>;
          }
          return (
            <span
              className={cx(
                'font-mono text-2xs tabular-nums',
                value >= 80 ? 'text-bad-400' : value >= 40 ? 'text-warn-400' : 'text-ink-200',
              )}
            >
              {fmtNum(value, 1)}%
            </span>
          );
        },
      },
      {
        key: 'memory',
        header: 'Memory',
        align: 'right' as const,
        render: (row: ContainerInfo) => {
          if (!isRunning(row)) return <span className="text-2xs text-ink-400">-</span>;
          const entry = statsById[row.id];
          if (!entry) return <span className="text-2xs text-ink-400">{stats.loading ? '…' : '-'}</span>;
          const used = entry.mem_usage_mb ?? null;
          const limit = entry.mem_limit_mb ?? null;
          const pct = used !== null && limit ? (used / limit) * 100 : null;
          return (
            <div className="text-right">
              <span
                className={cx(
                  'font-mono text-2xs tabular-nums',
                  pct !== null && pct >= 90 ? 'text-bad-400' : pct !== null && pct >= 70 ? 'text-warn-400' : 'text-ink-200',
                )}
              >
                {fmtNum(used, 0)}
                {limit ? ` / ${fmtNum(limit, 0)}` : ''} MB
              </span>
              {(entry.net_rx ?? null) !== null && (
                <p className="font-mono text-2xs text-ink-400">
                  ↓{fmtBytes(entry.net_rx)} ↑{fmtBytes(entry.net_tx)}
                </p>
              )}
            </div>
          );
        },
      },
      {
        key: 'actions',
        header: 'Actions',
        align: 'right' as const,
        render: (row: ContainerInfo) => {
          const running = isRunning(row);
          const busy = anyPending(row);
          return (
            <div className="flex flex-wrap items-center justify-end gap-1.5">
              <Button
                size="sm"
                variant="ghost"
                onClick={() => {
                  logStream.clear();
                  setLogTarget(row);
                }}
                disabled={busy}
                title="Stream this container's logs"
              >
                Logs
              </Button>
              {running ? (
                <>
                  <Button
                    size="sm"
                    variant="secondary"
                    onClick={() => void runRowAction(row, 'restart')}
                    busy={isPending(row, 'restart')}
                    disabled={busy}
                  >
                    Restart
                  </Button>
                  <Button
                    size="sm"
                    variant="secondary"
                    onClick={() => void runRowAction(row, 'stop')}
                    busy={isPending(row, 'stop')}
                    disabled={busy}
                  >
                    Stop
                  </Button>
                </>
              ) : (
                <Button
                  size="sm"
                  variant="secondary"
                  onClick={() => void runRowAction(row, 'start')}
                  busy={isPending(row, 'start')}
                  disabled={busy}
                >
                  Start
                </Button>
              )}
              <Button
                size="sm"
                variant="danger"
                onClick={() => setRemoveTarget(row)}
                busy={isPending(row, 'remove')}
                disabled={busy}
              >
                Remove
              </Button>
            </div>
          );
        },
      },
    ],
    // `logStream` and the action helpers are stable enough; statsById changes on
    // every stats poll, which is exactly when the CPU/memory cells must re-render.
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [statsById, stats.loading, pending, runRowAction],
  );

  const removeIsRunning = removeTarget ? isRunning(removeTarget) : false;
  const removeIsCritical = removeTarget ? isCritical(removeTarget) : false;

  return (
    <div className="space-y-6">
      {/* ------------------------------------------------------------ header */}
      <header className="flex flex-wrap items-end justify-between gap-3">
        <div>
          <h1 className="text-xl font-semibold tracking-tight text-ink-100">Containers</h1>
          <p className="mt-1 text-sm text-ink-400">
            Everything the Docker daemon exposes to hermes-core, plus a hardened scratch container
            you can run code in.
          </p>
        </div>
        <div className="flex items-center gap-2">
          {list.refreshing && <Spinner size="sm" label="Refreshing" />}
          <Button
            variant="ghost"
            onClick={() => {
              list.reload();
              stats.reload();
            }}
            disabled={list.refreshing}
          >
            Refresh
          </Button>
        </div>
      </header>

      {!dockerOk && <DockerBanner detail={list.data?.detail ?? ''} />}

      {actionError && (
        <ErrorState title="Container action failed" message={actionError} onRetry={() => list.reload()} />
      )}

      {actionNote && !actionError && (
        <div
          role="status"
          className="flex items-center justify-between gap-3 rounded-lg border border-good-600/40 bg-good-500/10 px-3 py-2 text-xs text-good-400"
        >
          <span>{actionNote}</span>
          <button
            type="button"
            onClick={() => setActionNote(null)}
            className="text-ink-300 hover:text-ink-100"
            aria-label="Dismiss"
          >
            ✕
          </button>
        </div>
      )}

      {/* -------------------------------------------------------- container table */}
      <Card
        title="Docker containers"
        subtitle={
          dockerOk
            ? `${containers.length} visible · list every ${LIST_POLL_MS / 1000}s, stats every ${STATS_POLL_MS / 1000}s`
            : 'Docker unavailable'
        }
      >
        {list.error ? (
          <ErrorState
            title="Could not list containers"
            message={list.error}
            onRetry={() => list.reload()}
          />
        ) : list.loading ? (
          <Spinner label="Talking to the Docker daemon" />
        ) : containers.length === 0 ? (
          <EmptyState
            title={dockerOk ? 'No containers visible' : 'Docker is not connected'}
            description={
              dockerOk
                ? 'hermes-core reached the daemon but it reported no containers. Start the stack with `make up`.'
                : 'Mount /var/run/docker.sock into hermes-core, then run `make restart`.'
            }
          />
        ) : (
          <DataTable
            rows={containers}
            columns={columns}
            rowKey={(row: ContainerInfo) => row.id}
            dense
          />
        )}
      </Card>

      {/* -------------------------------------------------------- sandbox exec */}
      <SandboxPanel dockerOk={dockerOk} />

      {/* --------------------------------------------------------- log drawer */}
      <Drawer
        open={logTarget !== null}
        title={logTarget ? `Logs — ${displayName(logTarget)}` : 'Logs'}
        subtitle={
          logTarget
            ? `${shortId(logTarget.id)} · last ${LOG_TAIL} lines then live`
            : undefined
        }
        onClose={() => setLogTarget(null)}
      >
        <LogStream
          lines={logStream.lines}
          connected={logStream.connected}
          failed={logStream.failed}
          onClear={logStream.clear}
          onReconnect={logStream.reconnect}
          emptyText="Waiting for output — a quiet container produces no lines."
          className="h-full"
        />
      </Drawer>

      {/* ------------------------------------------------------ remove confirm */}
      <ConfirmDialog
        open={removeTarget !== null}
        tone="danger"
        title={removeTarget ? `Remove ${displayName(removeTarget)}?` : 'Remove container?'}
        message={
          removeTarget
            ? [
                `This deletes the container${removeIsRunning ? ' (it is running, so it will be force-killed)' : ''}.`,
                'Named volumes and images are left alone, so `make up` can recreate it.',
                removeIsCritical
                  ? 'WARNING: this container runs the Hermes API or dashboard. Removing it will end this session — you will have to bring the stack back up from a terminal.'
                  : '',
              ]
                .filter(Boolean)
                .join(' ')
            : ''
        }
        confirmLabel={removeIsRunning ? 'Force remove' : 'Remove'}
        cancelLabel="Cancel"
        busy={removeTarget ? isPending(removeTarget, 'remove') : false}
        onConfirm={() => {
          const target = removeTarget;
          setRemoveTarget(null);
          if (target) void runRowAction(target, 'remove', isRunning(target));
        }}
        onCancel={() => setRemoveTarget(null)}
      />
    </div>
  );
}
