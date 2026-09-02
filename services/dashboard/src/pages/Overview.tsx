/**
 * Overview — the landing dashboard.
 *
 * Everything on this page is derived from live API data; nothing is faked and
 * nothing is hard-coded. It answers four questions in one screen:
 *
 *   1. Are my three dependencies healthy?      (LLM router / LinkedIn / Docker)
 *   2. How much work has Hermes actually done? (jobs by status, resumes, runs)
 *   3. What should I look at next?             (top-scoring jobs, recent runs)
 *   4. What is still not set up?               ("Getting started" checklist)
 *
 * IMPORTANT HONESTY CONSTRAINT: linkedin-mcp exposes no apply/submit tool.
 * Hermes ranks and tailors; the human clicks the apply link. Copy on this page
 * must never imply auto-submission.
 */

import { useMemo } from 'react';
import { Link } from 'react-router-dom';

import { Badge, Card, EmptyState, ErrorState, ScoreGauge, Spinner, StatusDot } from '../components';
import { api } from '../lib/api';
import { cx, fmtDuration, fmtRelative, scoreTone, truncate } from '../lib/format';
import { useApi } from '../lib/hooks';
import {
  JOB_STATUSES,
  RUN_KIND_LABELS,
  type Health,
  type Job,
  type JobStatus,
  type Run,
  type RunKind,
} from '../lib/types';

/* -------------------------------------------------------------------------- */
/* Shared bits                                                                 */
/* -------------------------------------------------------------------------- */

const LINK_CLS =
  'inline-flex items-center gap-1 rounded-md border border-ink-700 bg-ink-800 px-2.5 py-1 text-xs ' +
  'font-medium text-ink-100 transition-colors hover:border-brand-600 hover:text-brand-200 ' +
  'focus:outline-none focus-visible:ring-2 focus-visible:ring-brand-500';

type Tone = 'good' | 'warn' | 'bad' | 'neutral' | 'info';

const HEALTH_POLL_MS = 15_000;
const DATA_POLL_MS = 30_000;

/** Poll counts less aggressively than health; both are cheap but the DB isn't free. */
function runTone(status: string | undefined): Tone {
  switch ((status ?? '').toLowerCase()) {
    case 'done':
      return 'good';
    case 'error':
      return 'bad';
    case 'running':
      return 'info';
    case 'pending':
      return 'warn';
    default:
      return 'neutral';
  }
}

function statusTone(status: JobStatus): Tone {
  switch (status) {
    case 'applied':
      return 'good';
    case 'tailored':
      return 'info';
    case 'shortlisted':
      return 'warn';
    case 'rejected':
    case 'skipped':
      return 'bad';
    default:
      return 'neutral';
  }
}

/** The LinkedIn posting URL, reconstructed from the job id when `url` is absent. */
function jobUrl(job: Job): string | null {
  const direct = (job.url ?? '').trim();
  if (direct) return direct;
  const jobId = (job.linkedin_job_id ?? '').trim();
  if (jobId) return `https://www.linkedin.com/jobs/view/${encodeURIComponent(jobId)}/`;
  return null;
}

function runLabel(run: Run): string {
  const kind = (run.kind ?? '') as RunKind;
  return RUN_KIND_LABELS[kind] ?? String(run.kind ?? 'run');
}

/**
 * Read an undeclared key off a loosely-typed payload.
 *
 * `GET /api/health` returns more than `Health` declares (e.g. `docker_detail`,
 * `llm.reachable`), and hermes-core is free to add fields. This keeps those reads
 * honest instead of lying with a direct cast.
 */
function pick<T>(source: unknown, key: string): T | undefined {
  if (source && typeof source === 'object' && key in source) {
    return (source as Record<string, unknown>)[key] as T;
  }
  return undefined;
}

/* -------------------------------------------------------------------------- */
/* Health tiles                                                                */
/* -------------------------------------------------------------------------- */

interface TileSpec {
  key: string;
  label: string;
  tone: Tone;
  value: string;
  detail: string;
  to: string;
  cta: string;
}

function buildTiles(health: Health | null): TileSpec[] {
  const llm = health?.llm;
  const mcp = health?.mcp;
  const dockerContainers = pick<number>(pick<unknown>(health, 'docker_detail'), 'containers');
  const dockerReason = pick<string>(pick<unknown>(health, 'docker_detail'), 'detail');

  const llmReachable = Boolean(pick<boolean>(llm, 'reachable') ?? llm?.ok);
  const llmKey = Boolean(llm?.key_configured);
  const rawModels = llm?.models;
  const modelCount =
    typeof rawModels === 'number' ? rawModels : Array.isArray(rawModels) ? rawModels.length : 0;

  let llmTone: Tone = 'bad';
  let llmValue = 'Unreachable';
  if (!llmKey) {
    llmTone = 'warn';
    llmValue = 'No API key';
  } else if (llmReachable) {
    llmTone = 'good';
    llmValue = llm?.primary ? String(llm.primary) : `${modelCount} model${modelCount === 1 ? '' : 's'}`;
  }

  let mcpTone: Tone = 'bad';
  let mcpValue = 'Unreachable';
  if (mcp?.authenticated) {
    mcpTone = 'good';
    mcpValue = 'Session active';
  } else if (mcp?.reachable) {
    mcpTone = 'warn';
    mcpValue = 'Not connected';
  }

  const dockerOk = Boolean(health?.docker);

  return [
    {
      key: 'llm',
      label: 'LLM router',
      tone: llmTone,
      value: llmValue,
      detail:
        llm?.detail ||
        llm?.error ||
        (llmKey ? `${llm?.base_url ?? 'freellmapi'}` : 'Set FREELLMAPI_KEY to enable every agent.'),
      to: '/settings',
      cta: 'Settings',
    },
    {
      key: 'linkedin',
      label: 'LinkedIn session',
      tone: mcpTone,
      value: mcpValue,
      detail:
        mcp?.detail ||
        (mcp?.authenticated
          ? 'linkedin-mcp is signed in.'
          : 'Sign in by hand once — Hermes cannot log in for you.'),
      to: '/linkedin',
      cta: 'LinkedIn',
    },
    {
      key: 'docker',
      label: 'Docker / sandbox',
      tone: dockerOk ? 'good' : 'bad',
      value: dockerOk
        ? `${dockerContainers ?? 0} container${dockerContainers === 1 ? '' : 's'}`
        : 'Unavailable',
      detail:
        dockerReason ||
        (dockerOk
          ? 'docker.sock is mounted; the sandbox can run.'
          : 'Mount /var/run/docker.sock into hermes-core.'),
      to: '/containers',
      cta: 'Containers',
    },
  ];
}

function HealthTile({ tile, loading }: { tile: TileSpec; loading: boolean }) {
  return (
    <Card className="h-full">
      <div className="flex items-start justify-between gap-3">
        <div className="min-w-0">
          <div className="flex items-center gap-2">
            <StatusDot tone={tile.tone} pulse={tile.tone === 'info'} />
            <span className="text-2xs font-semibold uppercase tracking-wider text-ink-400">
              {tile.label}
            </span>
          </div>
          <p className="mt-2 truncate text-lg font-semibold text-ink-100" title={tile.value}>
            {loading ? <Spinner size="sm" /> : tile.value}
          </p>
          <p className="mt-1 line-clamp-2 text-xs leading-relaxed text-ink-400" title={tile.detail}>
            {truncate(tile.detail, 140) || ' '}
          </p>
        </div>
        <Link to={tile.to} className={LINK_CLS}>
          {tile.cta}
        </Link>
      </div>
    </Card>
  );
}

/* -------------------------------------------------------------------------- */
/* Getting started checklist                                                   */
/* -------------------------------------------------------------------------- */

interface ChecklistItem {
  key: string;
  done: boolean;
  title: string;
  hint: string;
  to: string;
  cta: string;
}

function ChecklistRow({ item, index }: { item: ChecklistItem; index: number }) {
  return (
    <li className="flex items-start gap-3 py-2.5">
      <span
        aria-hidden="true"
        className={cx(
          'mt-0.5 flex h-5 w-5 shrink-0 items-center justify-center rounded-full border text-2xs font-bold',
          item.done
            ? 'border-good-500 bg-good-500/15 text-good-400'
            : 'border-ink-600 bg-ink-800 text-ink-400',
        )}
      >
        {item.done ? '✓' : index + 1}
      </span>
      <div className="min-w-0 flex-1">
        <p className={cx('text-sm font-medium', item.done ? 'text-ink-300 line-through' : 'text-ink-100')}>
          {item.title}
        </p>
        {!item.done && <p className="mt-0.5 text-xs text-ink-400">{item.hint}</p>}
      </div>
      {!item.done && (
        <Link to={item.to} className={cx(LINK_CLS, 'shrink-0')}>
          {item.cta}
        </Link>
      )}
      <span className="sr-only">{item.done ? 'complete' : 'incomplete'}</span>
    </li>
  );
}

/* -------------------------------------------------------------------------- */
/* Page                                                                        */
/* -------------------------------------------------------------------------- */

export default function Overview() {
  const health = useApi<Health>((signal) => api.health(signal), [], { intervalMs: HEALTH_POLL_MS });
  const profile = useApi((signal) => api.getProfile(signal), [], { intervalMs: DATA_POLL_MS });
  const resumes = useApi((signal) => api.listResumes(signal), [], { intervalMs: DATA_POLL_MS });
  const jobs = useApi((signal) => api.listJobs(undefined, signal), [], { intervalMs: DATA_POLL_MS });
  const runs = useApi((signal) => api.listRuns(signal), [], { intervalMs: DATA_POLL_MS });

  const jobRows = useMemo(() => jobs.data ?? [], [jobs.data]);
  const resumeRows = useMemo(() => resumes.data ?? [], [resumes.data]);
  const runRows = useMemo(() => runs.data ?? [], [runs.data]);

  const tiles = useMemo(() => buildTiles(health.data), [health.data]);

  const statusCounts = useMemo(() => {
    const counts = new Map<JobStatus, number>();
    for (const status of JOB_STATUSES) counts.set(status, 0);
    for (const job of jobRows) {
      const status = (job.status ?? 'new') as JobStatus;
      counts.set(status, (counts.get(status) ?? 0) + 1);
    }
    return counts;
  }, [jobRows]);

  const topJobs = useMemo(
    () =>
      jobRows
        .filter((job) => typeof job.match_score === 'number')
        .sort((a, b) => (b.match_score ?? 0) - (a.match_score ?? 0))
        .slice(0, 5),
    [jobRows],
  );

  const recentRuns = useMemo(
    () =>
      runRows
        .slice()
        .sort((a, b) => {
          const left = a.started_at ?? '';
          const right = b.started_at ?? '';
          if (left === right) return String(b.id).localeCompare(String(a.id));
          return right.localeCompare(left);
        })
        .slice(0, 6),
    [runRows],
  );

  const bestAts = useMemo(() => {
    let best: number | null = null;
    for (const resume of resumeRows) {
      if (typeof resume.ats_score === 'number' && (best === null || resume.ats_score > best)) {
        best = resume.ats_score;
      }
    }
    return best;
  }, [resumeRows]);

  const checklist = useMemo<ChecklistItem[]>(() => {
    const llmReady = Boolean(health.data?.llm?.key_configured);
    const linkedInReady = Boolean(health.data?.mcp?.authenticated);
    const profileReady = Boolean(profile.data?.profile);
    return [
      {
        key: 'llm',
        done: llmReady,
        title: 'Configure the freellmapi key',
        hint: 'Mint a client token in the freellmapi dashboard on port 3001, then save it here.',
        to: '/settings',
        cta: 'Settings',
      },
      {
        key: 'linkedin',
        done: linkedInReady,
        title: 'Connect LinkedIn',
        hint: 'Run make login-linkedin and sign in by hand in the noVNC viewer.',
        to: '/linkedin',
        cta: 'Connect',
      },
      {
        key: 'profile',
        done: profileReady,
        title: 'Import your LinkedIn profile',
        hint: 'Hermes analyses it once to build your skill and keyword bank.',
        to: '/linkedin',
        cta: 'Import',
      },
      {
        key: 'resume',
        done: resumeRows.length > 0,
        title: 'Generate a base resume',
        hint: 'Produces an ATS-safe .docx/.pdf/.txt you can download.',
        to: '/resumes',
        cta: 'Resumes',
      },
      {
        key: 'jobs',
        done: jobRows.length > 0,
        title: 'Search for jobs',
        hint: 'Keywords are required; Hermes ranks the results against your profile.',
        to: '/jobs',
        cta: 'Search',
      },
    ];
  }, [health.data, profile.data, resumeRows.length, jobRows.length]);

  const doneCount = checklist.filter((item) => item.done).length;
  const allDone = doneCount === checklist.length;

  const reloadAll = () => {
    health.reload();
    profile.reload();
    resumes.reload();
    jobs.reload();
    runs.reload();
  };

  const anyRefreshing =
    health.refreshing || profile.refreshing || resumes.refreshing || jobs.refreshing || runs.refreshing;

  return (
    <div className="space-y-6">
      {/* ------------------------------------------------------------ header */}
      <header className="flex flex-wrap items-end justify-between gap-3">
        <div>
          <h1 className="text-xl font-semibold tracking-tight text-ink-100">Overview</h1>
          <p className="mt-1 text-sm text-ink-400">
            Hermes ranks jobs and tailors resumes. It never submits an application for you — every
            match links out to the real posting.
          </p>
        </div>
        <button
          type="button"
          onClick={reloadAll}
          className={LINK_CLS}
          disabled={anyRefreshing}
          aria-label="Refresh all dashboard data"
        >
          {anyRefreshing ? <Spinner size="sm" /> : null}
          <span>Refresh</span>
        </button>
      </header>

      {/* ------------------------------------------------------- health tiles */}
      <section aria-label="Dependency health">
        {health.error ? (
          <ErrorState
            title="Cannot reach hermes-core"
            message={health.error}
            onRetry={() => health.reload()}
          />
        ) : (
          <div className="grid grid-cols-1 gap-4 md:grid-cols-3">
            {tiles.map((tile) => (
              <HealthTile key={tile.key} tile={tile} loading={health.loading} />
            ))}
          </div>
        )}
      </section>

      {/* ------------------------------------------------------------ counts */}
      <section aria-label="Pipeline counts" className="grid grid-cols-1 gap-4 lg:grid-cols-3">
        <Card title="Jobs" className="lg:col-span-2">
          {jobs.error ? (
            <ErrorState message={jobs.error} onRetry={() => jobs.reload()} />
          ) : jobs.loading ? (
            <Spinner label="Loading jobs" />
          ) : (
            <>
              <div className="flex items-baseline gap-2">
                <span className="text-3xl font-semibold tabular-nums text-ink-100">{jobRows.length}</span>
                <span className="text-xs text-ink-400">discovered</span>
              </div>
              <div className="mt-3 flex flex-wrap gap-2">
                {JOB_STATUSES.map((status) => {
                  const count = statusCounts.get(status) ?? 0;
                  return (
                    <Badge key={status} tone={count > 0 ? statusTone(status) : 'neutral'}>
                      <span className="tabular-nums">{count}</span>
                      <span className="ml-1 opacity-80">{status}</span>
                    </Badge>
                  );
                })}
              </div>
            </>
          )}
        </Card>

        <Card title="Resumes & runs">
          <div className="grid grid-cols-2 gap-4">
            <div>
              <p className="text-3xl font-semibold tabular-nums text-ink-100">
                {resumes.loading ? <Spinner size="sm" /> : resumeRows.length}
              </p>
              <p className="mt-1 text-xs text-ink-400">resume versions</p>
              {resumes.error && <p className="mt-1 text-2xs text-bad-400">{resumes.error}</p>}
            </div>
            <div>
              <p className="text-3xl font-semibold tabular-nums text-ink-100">
                {runs.loading ? <Spinner size="sm" /> : runRows.length}
              </p>
              <p className="mt-1 text-xs text-ink-400">pipeline runs</p>
              {runs.error && <p className="mt-1 text-2xs text-bad-400">{runs.error}</p>}
            </div>
          </div>
          <div className="mt-4 flex items-center gap-4 border-t border-ink-800 pt-4">
            <ScoreGauge score={bestAts} label="Best ATS" size="sm" />
            <p className="text-xs leading-relaxed text-ink-400">
              {bestAts === null
                ? 'No resume has been scored yet.'
                : 'Highest ATS score across every stored resume version.'}
            </p>
          </div>
        </Card>
      </section>

      {/* -------------------------------------------------- getting started */}
      <section aria-label="Getting started">
        <Card
          title="Getting started"
          subtitle={`${doneCount} of ${checklist.length} complete`}
          actions={
            allDone ? (
              <Badge tone="good">Ready</Badge>
            ) : (
              <Badge tone="warn">{checklist.length - doneCount} remaining</Badge>
            )
          }
        >
          <ul className="divide-y divide-ink-800">
            {checklist.map((item, index) => (
              <ChecklistRow key={item.key} item={item} index={index} />
            ))}
          </ul>
          {allDone && (
            <p className="mt-3 border-t border-ink-800 pt-3 text-xs text-ink-400">
              Everything is configured. Re-run a job search whenever you want fresh postings.
            </p>
          )}
        </Card>
      </section>

      {/* -------------------------------------------- top jobs + recent runs */}
      <section className="grid grid-cols-1 gap-4 lg:grid-cols-2">
        <Card
          title="Top matches"
          subtitle="Highest match score first"
          actions={
            <Link to="/jobs" className={LINK_CLS}>
              All jobs
            </Link>
          }
        >
          {jobs.error ? (
            <ErrorState message={jobs.error} onRetry={() => jobs.reload()} />
          ) : jobs.loading ? (
            <Spinner label="Loading jobs" />
          ) : topJobs.length === 0 ? (
            <EmptyState
              title="No ranked jobs yet"
              description="Run a job search — Hermes scores every result against your imported profile."
              action={
                <Link to="/jobs" className={LINK_CLS}>
                  Search jobs
                </Link>
              }
            />
          ) : (
            <ul className="divide-y divide-ink-800">
              {topJobs.map((job) => {
                const href = jobUrl(job);
                const tone = scoreTone(job.match_score);
                return (
                  <li key={String(job.id)} className="flex items-center gap-3 py-2.5">
                    <span
                      className={cx(
                        'flex h-9 w-9 shrink-0 items-center justify-center rounded-md text-sm font-semibold tabular-nums',
                        tone === 'good' && 'bg-good-500/15 text-good-400',
                        tone === 'ok' && 'bg-warn-500/15 text-warn-400',
                        tone === 'weak' && 'bg-bad-500/15 text-bad-400',
                        tone === 'none' && 'bg-ink-800 text-ink-400',
                      )}
                      title="Match score"
                    >
                      {typeof job.match_score === 'number' ? Math.round(job.match_score) : '-'}
                    </span>
                    <div className="min-w-0 flex-1">
                      <Link
                        to={`/jobs?job=${encodeURIComponent(String(job.id))}`}
                        className="block truncate text-sm font-medium text-ink-100 hover:text-brand-300"
                        title={job.title ?? ''}
                      >
                        {job.title || 'Untitled role'}
                      </Link>
                      <p className="truncate text-xs text-ink-400">
                        {[job.company, job.location].filter(Boolean).join(' · ') || 'Unknown company'}
                      </p>
                    </div>
                    {href ? (
                      <a
                        href={href}
                        target="_blank"
                        rel="noopener noreferrer"
                        className={cx(LINK_CLS, 'shrink-0')}
                        title="Opens the LinkedIn posting — you apply there yourself"
                      >
                        Apply on LinkedIn
                      </a>
                    ) : (
                      <span className="shrink-0 text-2xs text-ink-400">no link</span>
                    )}
                  </li>
                );
              })}
            </ul>
          )}
          <p className="mt-3 border-t border-ink-800 pt-3 text-2xs leading-relaxed text-ink-400">
            Apply links open LinkedIn in a new tab. Hermes has no submit capability and will never
            send an application on your behalf.
          </p>
        </Card>

        <Card
          title="Recent runs"
          actions={
            <Link to="/runs" className={LINK_CLS}>
              All runs
            </Link>
          }
        >
          {runs.error ? (
            <ErrorState message={runs.error} onRetry={() => runs.reload()} />
          ) : runs.loading ? (
            <Spinner label="Loading runs" />
          ) : recentRuns.length === 0 ? (
            <EmptyState
              title="Nothing has run yet"
              description="Importing a profile or searching for jobs creates a run you can follow live."
            />
          ) : (
            <ul className="divide-y divide-ink-800">
              {recentRuns.map((run) => (
                <li key={String(run.id)} className="flex items-center gap-3 py-2.5">
                  <StatusDot
                    tone={runTone(run.status)}
                    pulse={(run.status ?? '') === 'running'}
                    label={String(run.status ?? 'unknown')}
                  />
                  <div className="min-w-0 flex-1">
                    <Link
                      to={`/runs/${encodeURIComponent(String(run.id))}`}
                      className="block truncate text-sm font-medium text-ink-100 hover:text-brand-300"
                    >
                      {runLabel(run)}
                    </Link>
                    <p className="truncate text-xs text-ink-400">
                      {fmtRelative(run.started_at)} · {fmtDuration(run.started_at, run.finished_at)}
                      {run.error ? ` · ${truncate(run.error, 60)}` : ''}
                    </p>
                  </div>
                  <Badge tone={runTone(run.status)}>{String(run.status ?? 'unknown')}</Badge>
                </li>
              ))}
            </ul>
          )}
        </Card>
      </section>
    </div>
  );
}
