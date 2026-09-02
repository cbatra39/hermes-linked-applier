/**
 * Jobs — the ranked LinkedIn job board.
 *
 * This is the payoff page of the whole product: postings scraped through the
 * LinkedIn MCP server, scored against the imported profile by MatchRanker, and
 * presented highest-match-first with an apply link.
 *
 * HONESTY CONTRACT (do not "improve" this away): the LinkedIn MCP server exposes
 * no apply/submit tool. Hermes ranks, tailors and tracks. The *human* opens the
 * posting and submits the application. Every action label and helper line on
 * this page has to keep saying that.
 *
 * Shared-UI prop contract assumed from '../components' (see the return notes to
 * the lead if any of these differ):
 *   Badge         {tone?: 'brand'|'good'|'warn'|'bad'|'info'|'neutral', title?, className?}
 *   Button        {variant?: 'primary'|'ghost'|'danger', size?: 'sm'|'md', className?, ...button}
 *   Card          {title?, subtitle?, actions?, className?, bodyClassName?}
 *   ConfirmDialog {open, title, message, confirmLabel?, cancelLabel?, onConfirm, onCancel}
 *   DataTable     {rows, columns:{key,header,render,align?,width?,className?}[], rowKey, onRowClick?, loading?, empty?, dense?}
 *   Drawer        {open, onClose, title, subtitle?, footer?}
 *   EmptyState    {title, message?, action?}
 *   ErrorState    {title?, message, onRetry?}
 *   KeywordChips  {items: string[], tone?: 'good'|'bad'|'neutral', emptyText?}
 *   LogStream     {lines, connected?, failed?, onReconnect?, className?}
 *   Modal         {open, onClose, title, footer?}
 *   Spinner       {} (no required props)
 *   StatusSelect  {value: JobStatus, onChange: (next: JobStatus) => void, disabled?, className?}
 */

import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { Link } from 'react-router-dom';

import {
  Badge,
  Button,
  Card,
  ConfirmDialog,
  DataTable,
  Drawer,
  EmptyState,
  ErrorState,
  KeywordChips,
  LogStream,
  Modal,
  Spinner,
  StatusSelect,
} from '../components';
import { api, errorMessage, jobBreakdown, parseJsonish } from '../lib/api';
import {
  clamp,
  cx,
  downloadText,
  fileStamp,
  fmtRelative,
  scoreTone,
  toCsv,
  type CsvColumn,
} from '../lib/format';
import { useApi, useDebounced, useLocalState, useRunWatch, useStream } from '../lib/hooks';
import {
  JOB_STATUSES,
  type Id,
  type Job,
  type JobSearchParams,
  type JobStatus,
  type MatchBreakdown,
} from '../lib/types';

/* -------------------------------------------------------------------------- */
/* Wire-shape adapters                                                        */
/* -------------------------------------------------------------------------- */

/**
 * hermes-core serialises Job through schemas.JobOut, which flattens the JSON
 * columns: the wire field is `match_breakdown` (an object) and it also carries
 * `apply_url`, neither of which exists on the `Job` interface in lib/types.ts
 * (that one mirrors the DB row: `match_breakdown_json`). Read both.
 */
type JobWire = Job & {
  match_breakdown?: unknown;
  apply_url?: string | null;
};

function matchOf(job: Job | null | undefined): MatchBreakdown | null {
  if (!job) return null;
  return jobBreakdown(job) ?? parseJsonish<MatchBreakdown>((job as JobWire).match_breakdown);
}

/**
 * Job URLs come from a scraper, so they are untrusted input. Only ever hand an
 * http(s) URL to an <a href>; anything else (javascript:, data:) is dropped.
 */
function safeHttpUrl(raw: string | null | undefined): string | null {
  if (!raw) return null;
  const text = String(raw).trim();
  if (!text) return null;
  try {
    const url = new URL(text, window.location.origin);
    if (url.protocol !== 'http:' && url.protocol !== 'https:') return null;
    return url.toString();
  } catch {
    return null;
  }
}

function applyUrlOf(job: Job): string | null {
  const wire = job as JobWire;
  return safeHttpUrl(wire.apply_url) ?? safeHttpUrl(job.url);
}

function statusOf(job: Job): JobStatus {
  const value = (job.status ?? 'new') as JobStatus;
  return JOB_STATUSES.includes(value) ? value : 'new';
}

/* -------------------------------------------------------------------------- */
/* Search-form option lists (values per schemas.JobSearchRequest comments)     */
/* -------------------------------------------------------------------------- */

interface Option {
  value: string;
  label: string;
}

const DATE_POSTED_OPTIONS: Option[] = [
  { value: '', label: 'Any time' },
  { value: 'past-24h', label: 'Past 24 hours' },
  { value: 'past-week', label: 'Past week' },
  { value: 'past-month', label: 'Past month' },
];

const JOB_TYPE_OPTIONS: Option[] = [
  { value: '', label: 'Any type' },
  { value: 'full-time', label: 'Full-time' },
  { value: 'part-time', label: 'Part-time' },
  { value: 'contract', label: 'Contract' },
  { value: 'temporary', label: 'Temporary' },
  { value: 'internship', label: 'Internship' },
  { value: 'volunteer', label: 'Volunteer' },
];

const EXPERIENCE_OPTIONS: Option[] = [
  { value: '', label: 'Any level' },
  { value: 'internship', label: 'Internship' },
  { value: 'entry', label: 'Entry level' },
  { value: 'associate', label: 'Associate' },
  { value: 'mid-senior', label: 'Mid-senior' },
  { value: 'director', label: 'Director' },
  { value: 'executive', label: 'Executive' },
];

const WORK_TYPE_OPTIONS: Option[] = [
  { value: '', label: 'Any workplace' },
  { value: 'on-site', label: 'On-site' },
  { value: 'remote', label: 'Remote' },
  { value: 'hybrid', label: 'Hybrid' },
];

const SORT_OPTIONS: Option[] = [
  { value: '', label: 'LinkedIn default' },
  { value: 'relevance', label: 'Relevance' },
  { value: 'date', label: 'Most recent' },
];

const STATUS_LABELS: Record<JobStatus, string> = {
  new: 'New',
  shortlisted: 'Shortlisted',
  tailored: 'Tailored',
  applied: 'Applied',
  rejected: 'Rejected',
  skipped: 'Skipped',
};

interface SearchForm {
  keywords: string;
  location: string;
  max_pages: number;
  date_posted: string;
  job_type: string;
  experience_level: string;
  work_type: string;
  easy_apply: boolean;
  sort_by: string;
  fetch_details: boolean;
}

const DEFAULT_SEARCH: SearchForm = {
  keywords: '',
  location: '',
  max_pages: 3,
  date_posted: '',
  job_type: '',
  experience_level: '',
  work_type: '',
  easy_apply: false,
  sort_by: '',
  fetch_details: true,
};

/* -------------------------------------------------------------------------- */
/* Small local presentation pieces                                            */
/* -------------------------------------------------------------------------- */

const BADGE_TONE: Record<string, 'good' | 'warn' | 'bad' | 'neutral'> = {
  good: 'good',
  ok: 'warn',
  weak: 'bad',
  none: 'neutral',
};

function ScoreBadge({ score, verdict }: { score: number | null | undefined; verdict?: string }) {
  const tone = BADGE_TONE[scoreTone(score)] ?? 'neutral';
  const has = score !== null && score !== undefined && !Number.isNaN(score);
  return (
    <Badge
      tone={tone}
      className="min-w-[3rem] justify-center font-mono tabular-nums"
      title={
        has
          ? `MatchRanker score ${Math.round(score)}/100${verdict ? ` — ${verdict}` : ''}`
          : 'Not scored yet — import your profile, then re-run the search to rank these postings.'
      }
    >
      {has ? Math.round(score) : '—'}
    </Badge>
  );
}

function LabeledField({
  label,
  hint,
  children,
  className,
}: {
  label: string;
  hint?: string;
  children: React.ReactNode;
  className?: string;
}) {
  return (
    <label className={cx('flex flex-col gap-1', className)}>
      <span className="text-2xs font-medium uppercase tracking-wide text-ink-300">{label}</span>
      {children}
      {hint ? <span className="text-2xs text-ink-400">{hint}</span> : null}
    </label>
  );
}

const CONTROL_CLASS =
  'w-full rounded-md border border-ink-600 bg-ink-850 px-2.5 py-1.5 text-sm text-ink-100 ' +
  'placeholder:text-ink-400 focus:border-brand-400 focus:outline-none focus:ring-1 focus:ring-brand-400/40';

function SelectField({
  label,
  value,
  options,
  onChange,
}: {
  label: string;
  value: string;
  options: Option[];
  onChange: (next: string) => void;
}) {
  return (
    <LabeledField label={label}>
      <select className={CONTROL_CLASS} value={value} onChange={(event) => onChange(event.target.value)}>
        {options.map((option) => (
          <option key={option.value || '_any'} value={option.value}>
            {option.label}
          </option>
        ))}
      </select>
    </LabeledField>
  );
}

function BulletList({ title, items }: { title: string; items: string[] | undefined }) {
  if (!items || items.length === 0) return null;
  return (
    <div>
      <h4 className="mb-1 text-2xs font-semibold uppercase tracking-wide text-ink-300">{title}</h4>
      <ul className="space-y-1 text-sm text-ink-200">
        {items.map((item, index) => (
          <li key={`${title}-${index}`} className="flex gap-2">
            <span className="mt-[0.35rem] h-1 w-1 shrink-0 rounded-full bg-brand-400" aria-hidden />
            <span>{item}</span>
          </li>
        ))}
      </ul>
    </div>
  );
}

/* -------------------------------------------------------------------------- */
/* Page                                                                       */
/* -------------------------------------------------------------------------- */

type Notice = { tone: 'good' | 'bad'; text: string } | null;

/** What the live run panel is currently following. */
interface ActiveRun {
  id: Id;
  label: string;
}

export default function Jobs() {
  /* ---- data ------------------------------------------------------------ */
  const jobsQuery = useApi((signal) => api.listJobs(undefined, signal), []);
  const linkedin = useApi((signal) => api.linkedinStatus(signal), [], { intervalMs: 120_000 });

  const jobs = jobsQuery.data ?? [];
  const linkedinReady = linkedin.data?.authenticated === true;

  /* ---- filters (persisted) --------------------------------------------- */
  const [minScore, setMinScore] = useLocalState<number>('jobs.minScore', 0);
  const [statusFilter, setStatusFilter] = useLocalState<JobStatus[]>('jobs.statusFilter', []);
  const [easyOnly, setEasyOnly] = useLocalState<boolean>('jobs.easyOnly', false);
  const [query, setQuery] = useState('');
  const debouncedQuery = useDebounced(query, 200);

  /* ---- search form (persisted) ----------------------------------------- */
  const [storedSearch, setStoredSearch] = useLocalState<Partial<SearchForm>>('jobs.search', {});
  const [form, setForm] = useState<SearchForm>(() => ({ ...DEFAULT_SEARCH, ...storedSearch }));
  const [searchOpen, setSearchOpen] = useState(false);
  const [searching, setSearching] = useState(false);
  const [formError, setFormError] = useState<string | null>(null);

  const patchForm = useCallback(<K extends keyof SearchForm>(key: K, value: SearchForm[K]) => {
    setForm((previous) => ({ ...previous, [key]: value }));
  }, []);

  /* ---- ui state -------------------------------------------------------- */
  const [notice, setNotice] = useState<Notice>(null);
  const [openJobId, setOpenJobId] = useState<Id | null>(null);
  const [pendingStatusId, setPendingStatusId] = useState<string | null>(null);
  const [tailoringId, setTailoringId] = useState<string | null>(null);
  const [activeRun, setActiveRun] = useState<ActiveRun | null>(null);
  const [applyPrompt, setApplyPrompt] = useState<Job | null>(null);
  const [loginInfo, setLoginInfo] = useState<{ url: string | null; instructions: string } | null>(null);
  const [loginBusy, setLoginBusy] = useState(false);

  /* ---- live run log ---------------------------------------------------- */
  const stream = useStream(activeRun ? api.runEventsPath(activeRun.id) : null, 400);
  const clearStream = stream.clear;
  const reloadJobs = jobsQuery.reload;
  const { run: watchedRun } = useRunWatch(activeRun?.id ?? null, (finished) => {
    setSearching(false);
    setTailoringId(null);
    reloadJobs();
    if (finished.status === 'error') {
      setNotice({ tone: 'bad', text: finished.error || 'The run failed — see the log below.' });
    } else {
      setNotice({ tone: 'good', text: 'Run finished. The list below has been refreshed.' });
    }
  });

  /* ---- optimistic row patching ---------------------------------------- */
  const setJobs = jobsQuery.setData;
  const patchRow = useCallback(
    (id: Id, changes: Partial<Job>) => {
      setJobs((previous) =>
        (previous ?? []).map((job) => (String(job.id) === String(id) ? { ...job, ...changes } : job)),
      );
    },
    [setJobs],
  );

  const changeStatus = useCallback(
    async (job: Job, next: JobStatus) => {
      const key = String(job.id);
      const previous = statusOf(job);
      if (previous === next) return;

      setPendingStatusId(key);
      patchRow(job.id, { status: next });
      try {
        const updated = await api.patchJob(job.id, { status: next });
        // Trust the server's echo (it also stamps applied_at).
        if (updated && typeof updated === 'object' && 'id' in updated) {
          patchRow(job.id, updated);
        }
        setNotice({ tone: 'good', text: `"${job.title ?? 'Job'}" marked ${STATUS_LABELS[next]}.` });
      } catch (err) {
        patchRow(job.id, { status: previous }); // rollback
        setNotice({ tone: 'bad', text: `Status change failed: ${errorMessage(err)}` });
      } finally {
        setPendingStatusId((current) => (current === key ? null : current));
      }
    },
    [patchRow],
  );

  /* ---- debounced notes ------------------------------------------------- */
  const [notesState, setNotesState] = useState<Record<string, 'dirty' | 'saving' | 'saved' | 'failed'>>({});
  const notesTimers = useRef(new Map<string, number>());

  useEffect(() => {
    const timers = notesTimers.current;
    return () => {
      timers.forEach((handle) => window.clearTimeout(handle));
      timers.clear();
    };
  }, []);

  const flushNotes = useCallback(
    async (id: Id, text: string) => {
      const key = String(id);
      setNotesState((previous) => ({ ...previous, [key]: 'saving' }));
      try {
        await api.patchJob(id, { notes: text });
        setNotesState((previous) => ({ ...previous, [key]: 'saved' }));
      } catch (err) {
        setNotesState((previous) => ({ ...previous, [key]: 'failed' }));
        setNotice({ tone: 'bad', text: `Notes not saved: ${errorMessage(err)}` });
      }
    },
    [],
  );

  const editNotes = useCallback(
    (job: Job, text: string) => {
      const key = String(job.id);
      patchRow(job.id, { notes: text });
      setNotesState((previous) => ({ ...previous, [key]: 'dirty' }));

      const existing = notesTimers.current.get(key);
      if (existing !== undefined) window.clearTimeout(existing);
      const handle = window.setTimeout(() => {
        notesTimers.current.delete(key);
        void flushNotes(job.id, text);
      }, 800);
      notesTimers.current.set(key, handle);
    },
    [flushNotes, patchRow],
  );

  const saveNotesNow = useCallback(
    (job: Job) => {
      const key = String(job.id);
      const existing = notesTimers.current.get(key);
      if (existing !== undefined) {
        window.clearTimeout(existing);
        notesTimers.current.delete(key);
      }
      void flushNotes(job.id, job.notes ?? '');
    },
    [flushNotes],
  );

  /* ---- actions --------------------------------------------------------- */
  const runSearch = useCallback(async () => {
    const keywords = form.keywords.trim();
    if (!keywords) {
      setFormError('Keywords are required — the LinkedIn search tool rejects an empty query.');
      return;
    }
    setFormError(null);
    setStoredSearch(form);
    setSearching(true);
    clearStream();

    const params: JobSearchParams = {
      keywords,
      max_pages: clamp(Math.round(form.max_pages) || 1, 1, 10),
      easy_apply: form.easy_apply,
      fetch_details: form.fetch_details,
    };
    if (form.location.trim()) params.location = form.location.trim();
    if (form.date_posted) params.date_posted = form.date_posted;
    if (form.job_type) params.job_type = form.job_type;
    if (form.experience_level) params.experience_level = form.experience_level;
    if (form.work_type) params.work_type = form.work_type;
    if (form.sort_by) params.sort_by = form.sort_by;

    try {
      const run = await api.searchJobs(params);
      setActiveRun({ id: run.id, label: `Job search — "${keywords}"` });
      setNotice(null);
      setSearchOpen(false);
    } catch (err) {
      setSearching(false);
      setFormError(errorMessage(err));
    }
  }, [clearStream, form, setStoredSearch]);

  const tailorJob = useCallback(
    async (job: Job) => {
      const key = String(job.id);
      setTailoringId(key);
      clearStream();
      try {
        const run = await api.tailorJob(job.id);
        setActiveRun({ id: run.id, label: `Tailoring resume — ${job.title ?? 'job'} @ ${job.company ?? '?'}` });
        setNotice(null);
      } catch (err) {
        setTailoringId(null);
        setNotice({ tone: 'bad', text: `Tailoring could not start: ${errorMessage(err)}` });
      }
    },
    [clearStream],
  );

  const startLogin = useCallback(async () => {
    setLoginBusy(true);
    try {
      const response = await api.linkedinLogin();
      setLoginInfo({
        url: safeHttpUrl(response.viewer_url),
        instructions:
          response.instructions ||
          'Open the login viewer, sign in to LinkedIn, then close the viewer and refresh this page.',
      });
    } catch (err) {
      setNotice({ tone: 'bad', text: `Could not start the LinkedIn login flow: ${errorMessage(err)}` });
    } finally {
      setLoginBusy(false);
    }
  }, []);

  /* ---- filtering + ranking -------------------------------------------- */
  const filtered = useMemo(() => {
    const needle = debouncedQuery.trim().toLowerCase();
    const wanted = new Set(statusFilter);

    const rows = jobs.filter((job) => {
      const score = job.match_score;
      if (minScore > 0 && (score === null || score === undefined || score < minScore)) return false;
      if (wanted.size > 0 && !wanted.has(statusOf(job))) return false;
      if (easyOnly && !job.easy_apply) return false;
      if (needle) {
        const haystack = [job.title, job.company, job.location, job.notes, job.linkedin_job_id]
          .filter(Boolean)
          .join(' ')
          .toLowerCase();
        if (!haystack.includes(needle)) return false;
      }
      return true;
    });

    // Ranked list: match score descending, unscored last, then company A-Z.
    return rows.sort((a, b) => {
      const left = a.match_score ?? -1;
      const right = b.match_score ?? -1;
      if (right !== left) return right - left;
      return (a.company ?? '').localeCompare(b.company ?? '');
    });
  }, [debouncedQuery, easyOnly, jobs, minScore, statusFilter]);

  const openJob = useMemo(() => {
    if (openJobId === null) return null;
    return jobs.find((job) => String(job.id) === String(openJobId)) ?? null;
  }, [jobs, openJobId]);

  const scoredCount = useMemo(
    () => jobs.filter((job) => job.match_score !== null && job.match_score !== undefined).length,
    [jobs],
  );

  const filtersActive = minScore > 0 || statusFilter.length > 0 || easyOnly || debouncedQuery.trim() !== '';

  const clearFilters = useCallback(() => {
    setMinScore(0);
    setStatusFilter([]);
    setEasyOnly(false);
    setQuery('');
  }, [setEasyOnly, setMinScore, setStatusFilter]);

  const toggleStatus = useCallback(
    (status: JobStatus) => {
      const next = statusFilter.includes(status)
        ? statusFilter.filter((entry) => entry !== status)
        : [...statusFilter, status];
      setStatusFilter(next);
    },
    [setStatusFilter, statusFilter],
  );

  /* ---- CSV export ------------------------------------------------------ */
  const exportCsv = useCallback(() => {
    const columns: Array<CsvColumn<Job>> = [
      { header: 'match_score', value: (job) => job.match_score ?? '' },
      { header: 'verdict', value: (job) => matchOf(job)?.verdict ?? '' },
      { header: 'title', value: (job) => job.title ?? '' },
      { header: 'company', value: (job) => job.company ?? '' },
      { header: 'location', value: (job) => job.location ?? '' },
      { header: 'easy_apply', value: (job) => (job.easy_apply ? 'yes' : 'no') },
      { header: 'posted', value: (job) => job.posted ?? '' },
      { header: 'status', value: (job) => statusOf(job) },
      { header: 'discovered_at', value: (job) => job.discovered_at ?? '' },
      { header: 'applied_at', value: (job) => job.applied_at ?? '' },
      { header: 'matched_skills', value: (job) => (matchOf(job)?.matched_skills ?? []).join(' | ') },
      { header: 'missing_skills', value: (job) => (matchOf(job)?.missing_skills ?? []).join(' | ') },
      { header: 'notes', value: (job) => job.notes ?? '' },
      { header: 'linkedin_job_id', value: (job) => job.linkedin_job_id ?? '' },
      { header: 'apply_url', value: (job) => applyUrlOf(job) ?? '' },
      { header: 'tailored_resume_id', value: (job) => job.tailored_resume_id ?? '' },
      { header: 'hermes_job_id', value: (job) => job.id },
    ];
    downloadText(`hermes-jobs-${fileStamp()}.csv`, toCsv(filtered, columns), 'text/csv;charset=utf-8');
  }, [filtered]);

  /* ---- table columns --------------------------------------------------- */
  const columns = useMemo(
    () => [
      {
        key: 'score',
        header: 'Match',
        width: '84px',
        align: 'center' as const,
        render: (job: Job) => <ScoreBadge score={job.match_score} verdict={matchOf(job)?.verdict} />,
      },
      {
        key: 'role',
        header: 'Role',
        render: (job: Job) => (
          <div className="min-w-0">
            <div className="flex items-center gap-2">
              <span className="truncate font-medium text-ink-100">{job.title || 'Untitled posting'}</span>
              {job.easy_apply ? (
                <Badge tone="brand" title="LinkedIn marks this posting as Easy Apply.">
                  Easy Apply
                </Badge>
              ) : null}
            </div>
            <div className="truncate text-2xs text-ink-300">
              {job.company || 'Unknown company'}
              {job.location ? <span className="text-ink-400"> · {job.location}</span> : null}
            </div>
          </div>
        ),
      },
      {
        key: 'posted',
        header: 'Posted',
        width: '132px',
        render: (job: Job) => (
          <span className="text-2xs text-ink-300" title={`Discovered ${fmtRelative(job.discovered_at)}`}>
            {job.posted || fmtRelative(job.discovered_at)}
          </span>
        ),
      },
      {
        key: 'status',
        header: 'Status',
        width: '158px',
        render: (job: Job) => (
          <div onClick={(event) => event.stopPropagation()}>
            <StatusSelect
              value={statusOf(job)}
              disabled={pendingStatusId === String(job.id)}
              onChange={(next: JobStatus) => void changeStatus(job, next)}
              className="w-full"
            />
          </div>
        ),
      },
      {
        key: 'actions',
        header: 'Apply',
        width: '224px',
        align: 'right' as const,
        render: (job: Job) => {
          const href = applyUrlOf(job);
          const busy = tailoringId === String(job.id);
          return (
            <div className="flex items-center justify-end gap-1.5" onClick={(event) => event.stopPropagation()}>
              {href ? (
                <a
                  href={href}
                  target="_blank"
                  rel="noopener noreferrer"
                  onClick={() => setApplyPrompt(job)}
                  title="Opens the LinkedIn posting in a new tab. You review and submit it yourself — Hermes cannot submit applications."
                  className={
                    'inline-flex items-center gap-1 rounded-md bg-brand-500 px-2.5 py-1.5 text-2xs font-semibold ' +
                    'text-ink-950 transition-colors hover:bg-brand-400 focus:outline-none focus:ring-2 focus:ring-brand-400/50'
                  }
                >
                  Open &amp; Apply
                  <span aria-hidden>&#8599;</span>
                </a>
              ) : (
                <span className="text-2xs text-ink-400" title="This posting has no usable http(s) URL.">
                  No link
                </span>
              )}
              <Button
                variant="ghost"
                size="sm"
                disabled={busy || activeRun !== null}
                onClick={() => void tailorJob(job)}
                title="Build a resume version aimed at this job description, then score it."
              >
                {busy ? 'Tailoring…' : 'Tailor'}
              </Button>
            </div>
          );
        },
      },
    ],
    [activeRun, changeStatus, pendingStatusId, tailorJob, tailoringId],
  );

  /* ---- empty / error copy --------------------------------------------- */
  const emptyState = useMemo(() => {
    if (jobs.length > 0 && filtered.length === 0) {
      return (
        <EmptyState
          title="No postings match these filters"
          message={`${jobs.length} stored ${jobs.length === 1 ? 'posting is' : 'postings are'} hidden by the current filters.`}
          action={
            <Button variant="ghost" onClick={clearFilters}>
              Clear filters
            </Button>
          }
        />
      );
    }
    if (linkedin.data && !linkedinReady) {
      return (
        <EmptyState
          title="LinkedIn not connected"
          message={
            linkedin.data.detail?.trim() ||
            'The LinkedIn MCP container has no valid session, so no postings can be fetched. Sign in once and the session is reused.'
          }
          action={
            <Button variant="primary" disabled={loginBusy} onClick={() => void startLogin()}>
              {loginBusy ? 'Starting…' : 'Connect LinkedIn'}
            </Button>
          }
        />
      );
    }
    return (
      <EmptyState
        title="No jobs scouted yet"
        message="Run a LinkedIn search to pull postings in. They are ranked against your imported profile, highest match first."
        action={
          <Button variant="primary" onClick={() => setSearchOpen(true)}>
            Search LinkedIn jobs
          </Button>
        }
      />
    );
  }, [clearFilters, filtered.length, jobs.length, linkedin.data, linkedinReady, loginBusy, startLogin]);

  /* ---- render ---------------------------------------------------------- */
  return (
    <div className="flex flex-col gap-4">
      {/* honesty banner — required by the product contract */}
      <div className="flex items-start gap-2.5 rounded-lg border border-brand-800/70 bg-brand-800/15 px-3.5 py-2.5">
        <span aria-hidden className="mt-0.5 text-brand-300">
          &#9432;
        </span>
        <p className="text-xs leading-relaxed text-ink-200">
          <strong className="font-semibold text-brand-200">Hermes never submits applications.</strong> It scouts
          postings, scores them against your profile and tailors a resume. &ldquo;Open &amp; Apply&rdquo; opens the
          LinkedIn posting in a new tab — you review and submit it yourself, then set the status here to keep track.
        </p>
      </div>

      {notice ? (
        <div
          role="status"
          className={cx(
            'flex items-start justify-between gap-3 rounded-lg border px-3.5 py-2.5 text-xs',
            notice.tone === 'good'
              ? 'border-good-600/50 bg-good-600/10 text-good-400'
              : 'border-bad-600/50 bg-bad-600/10 text-bad-400',
          )}
        >
          <span>{notice.text}</span>
          <button type="button" className="shrink-0 text-ink-300 hover:text-ink-100" onClick={() => setNotice(null)}>
            Dismiss
          </button>
        </div>
      ) : null}

      {linkedin.data && !linkedinReady && jobs.length > 0 ? (
        <div className="flex flex-wrap items-center justify-between gap-3 rounded-lg border border-warn-600/40 bg-warn-600/10 px-3.5 py-2.5 text-xs text-warn-400">
          <span>
            LinkedIn not connected — the stored postings below may be stale and a new search will fail until you sign
            in.
          </span>
          <Button variant="ghost" size="sm" disabled={loginBusy} onClick={() => void startLogin()}>
            {loginBusy ? 'Starting…' : 'Connect LinkedIn'}
          </Button>
        </div>
      ) : null}

      {/* ---- search ------------------------------------------------------ */}
      <Card
        title="Search LinkedIn jobs"
        subtitle="Calls the linkedin-mcp search_jobs tool, stores every posting, then ranks the new ones against your profile."
        actions={
          <Button variant="ghost" size="sm" onClick={() => setSearchOpen((open) => !open)}>
            {searchOpen ? 'Hide form' : 'Show form'}
          </Button>
        }
      >
        {searchOpen ? (
          <form
            className="flex flex-col gap-3"
            onSubmit={(event) => {
              event.preventDefault();
              void runSearch();
            }}
          >
            <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-4">
              <LabeledField label="Keywords *" hint="Required by the LinkedIn tool." className="sm:col-span-2">
                <input
                  className={CONTROL_CLASS}
                  value={form.keywords}
                  required
                  placeholder="e.g. senior data engineer"
                  onChange={(event) => patchForm('keywords', event.target.value)}
                />
              </LabeledField>
              <LabeledField label="Location" hint="City, region or country. Blank = anywhere.">
                <input
                  className={CONTROL_CLASS}
                  value={form.location}
                  placeholder="e.g. Bengaluru, India"
                  onChange={(event) => patchForm('location', event.target.value)}
                />
              </LabeledField>
              <LabeledField label="Pages" hint="1–10. Each page is ~25 postings.">
                <input
                  className={CONTROL_CLASS}
                  type="number"
                  min={1}
                  max={10}
                  value={form.max_pages}
                  onChange={(event) => patchForm('max_pages', clamp(Number(event.target.value) || 1, 1, 10))}
                />
              </LabeledField>

              <SelectField
                label="Date posted"
                value={form.date_posted}
                options={DATE_POSTED_OPTIONS}
                onChange={(value) => patchForm('date_posted', value)}
              />
              <SelectField
                label="Job type"
                value={form.job_type}
                options={JOB_TYPE_OPTIONS}
                onChange={(value) => patchForm('job_type', value)}
              />
              <SelectField
                label="Experience level"
                value={form.experience_level}
                options={EXPERIENCE_OPTIONS}
                onChange={(value) => patchForm('experience_level', value)}
              />
              <SelectField
                label="Workplace"
                value={form.work_type}
                options={WORK_TYPE_OPTIONS}
                onChange={(value) => patchForm('work_type', value)}
              />
              <SelectField
                label="Sort by"
                value={form.sort_by}
                options={SORT_OPTIONS}
                onChange={(value) => patchForm('sort_by', value)}
              />
            </div>

            <div className="flex flex-wrap items-center gap-4">
              <label className="flex items-center gap-2 text-xs text-ink-200">
                <input
                  type="checkbox"
                  className="h-3.5 w-3.5 accent-brand-400"
                  checked={form.easy_apply}
                  onChange={(event) => patchForm('easy_apply', event.target.checked)}
                />
                Easy Apply postings only
              </label>
              <label className="flex items-center gap-2 text-xs text-ink-200">
                <input
                  type="checkbox"
                  className="h-3.5 w-3.5 accent-brand-400"
                  checked={form.fetch_details}
                  onChange={(event) => patchForm('fetch_details', event.target.checked)}
                />
                Fetch full descriptions
                <span className="text-ink-400">(slower, but required for good ranking and tailoring)</span>
              </label>
            </div>

            {formError ? <p className="text-xs text-bad-400">{formError}</p> : null}

            <div className="flex items-center gap-2">
              <Button type="submit" variant="primary" disabled={searching || !form.keywords.trim()}>
                {searching ? 'Searching…' : 'Search LinkedIn'}
              </Button>
              <Button
                type="button"
                variant="ghost"
                onClick={() => {
                  setForm({ ...DEFAULT_SEARCH });
                  setFormError(null);
                }}
              >
                Reset
              </Button>
              {searching ? <Spinner /> : null}
            </div>
          </form>
        ) : (
          <p className="text-xs text-ink-300">
            {form.keywords.trim()
              ? `Last query: "${form.keywords.trim()}"${form.location.trim() ? ` in ${form.location.trim()}` : ''}.`
              : 'No search run from this browser yet.'}
          </p>
        )}
      </Card>

      {/* ---- live run ---------------------------------------------------- */}
      {activeRun ? (
        <Card
          title={activeRun.label}
          subtitle={
            watchedRun?.status
              ? `Run ${activeRun.id} — ${watchedRun.status}`
              : `Run ${activeRun.id} — starting…`
          }
          actions={
            <div className="flex items-center gap-2">
              <Link to="/runs" className="text-2xs text-brand-300 underline-offset-2 hover:underline">
                All runs
              </Link>
              <Button variant="ghost" size="sm" onClick={() => setActiveRun(null)}>
                Close
              </Button>
            </div>
          }
        >
          <LogStream
            lines={stream.lines}
            connected={stream.connected}
            failed={stream.failed}
            onReconnect={stream.reconnect}
            className="h-56"
          />
          {watchedRun?.error ? <p className="mt-2 text-xs text-bad-400">{watchedRun.error}</p> : null}
        </Card>
      ) : null}

      {/* ---- filters ----------------------------------------------------- */}
      <Card
        title="Ranked postings"
        subtitle={`${filtered.length} of ${jobs.length} shown · ${scoredCount} scored by MatchRanker`}
        actions={
          <div className="flex items-center gap-2">
            {jobsQuery.refreshing ? <Spinner /> : null}
            <Button variant="ghost" size="sm" onClick={exportCsv} disabled={filtered.length === 0}>
              Export CSV
            </Button>
            <Button variant="ghost" size="sm" onClick={() => jobsQuery.reload()}>
              Refresh
            </Button>
          </div>
        }
      >
        <div className="mb-3 flex flex-col gap-3 border-b border-ink-700 pb-3">
          <div className="flex flex-wrap items-end gap-4">
            <LabeledField label="Search" className="min-w-[14rem] flex-1">
              <input
                className={CONTROL_CLASS}
                value={query}
                placeholder="Title, company, location or notes"
                onChange={(event) => setQuery(event.target.value)}
              />
            </LabeledField>

            <LabeledField label={`Min match score: ${minScore}`} className="min-w-[12rem]">
              <input
                type="range"
                min={0}
                max={100}
                step={5}
                value={minScore}
                className="w-full accent-brand-400"
                onChange={(event) => setMinScore(clamp(Number(event.target.value) || 0, 0, 100))}
              />
            </LabeledField>

            <label className="flex items-center gap-2 pb-1.5 text-xs text-ink-200">
              <input
                type="checkbox"
                className="h-3.5 w-3.5 accent-brand-400"
                checked={easyOnly}
                onChange={(event) => setEasyOnly(event.target.checked)}
              />
              Easy Apply only
            </label>

            {filtersActive ? (
              <Button variant="ghost" size="sm" className="mb-0.5" onClick={clearFilters}>
                Clear filters
              </Button>
            ) : null}
          </div>

          <div className="flex flex-wrap items-center gap-1.5">
            <span className="mr-1 text-2xs font-medium uppercase tracking-wide text-ink-300">Status</span>
            {JOB_STATUSES.map((status) => {
              const on = statusFilter.includes(status);
              const count = jobs.filter((job) => statusOf(job) === status).length;
              return (
                <button
                  key={status}
                  type="button"
                  onClick={() => toggleStatus(status)}
                  aria-pressed={on}
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
        </div>

        {jobsQuery.error ? (
          <ErrorState
            title="Could not load jobs"
            message={jobsQuery.error}
            onRetry={() => jobsQuery.reload()}
          />
        ) : (
          <DataTable
            rows={filtered}
            columns={columns}
            rowKey={(job: Job) => String(job.id)}
            loading={jobsQuery.loading}
            onRowClick={(job: Job) => setOpenJobId(job.id)}
            empty={emptyState}
            dense
          />
        )}
      </Card>

      {/* ---- detail drawer ---------------------------------------------- */}
      <Drawer
        open={openJob !== null}
        onClose={() => setOpenJobId(null)}
        title={openJob?.title || 'Job detail'}
        subtitle={openJob ? [openJob.company, openJob.location].filter(Boolean).join(' · ') : undefined}
      >
        {openJob ? <JobDetail
          job={openJob}
          breakdown={matchOf(openJob)}
          notesState={notesState[String(openJob.id)]}
          statusPending={pendingStatusId === String(openJob.id)}
          tailoring={tailoringId === String(openJob.id)}
          tailorDisabled={activeRun !== null}
          onStatus={(next) => void changeStatus(openJob, next)}
          onNotes={(text) => editNotes(openJob, text)}
          onSaveNotes={() => saveNotesNow(openJob)}
          onTailor={() => void tailorJob(openJob)}
          onApplyClick={() => setApplyPrompt(openJob)}
        /> : null}
      </Drawer>

      {/* ---- "did you submit it?" ---------------------------------------- */}
      <ConfirmDialog
        open={applyPrompt !== null}
        title="Opened in a new tab"
        message={
          applyPrompt
            ? `Hermes opened "${applyPrompt.title ?? 'the posting'}" on LinkedIn. It cannot submit the application for you. ` +
              'Once you have submitted it there, mark it Applied so it stops showing up as new.'
            : ''
        }
        confirmLabel="I submitted it — mark Applied"
        cancelLabel="Not yet"
        onConfirm={() => {
          const job = applyPrompt;
          setApplyPrompt(null);
          if (job) void changeStatus(job, 'applied');
        }}
        onCancel={() => setApplyPrompt(null)}
      />

      {/* ---- LinkedIn login instructions --------------------------------- */}
      <Modal
        open={loginInfo !== null}
        onClose={() => setLoginInfo(null)}
        title="Connect LinkedIn"
        footer={
          <Button
            variant="ghost"
            onClick={() => {
              setLoginInfo(null);
              linkedin.reload();
            }}
          >
            Done — re-check status
          </Button>
        }
      >
        <div className="flex flex-col gap-3 text-sm text-ink-200">
          <p className="whitespace-pre-line">{loginInfo?.instructions}</p>
          {loginInfo?.url ? (
            <a
              href={loginInfo.url}
              target="_blank"
              rel="noopener noreferrer"
              className="inline-flex w-fit items-center gap-1 rounded-md bg-brand-500 px-3 py-1.5 text-xs font-semibold text-ink-950 hover:bg-brand-400"
            >
              Open login viewer <span aria-hidden>&#8599;</span>
            </a>
          ) : (
            <p className="text-xs text-ink-400">
              No viewer URL was returned. Run <code className="font-mono text-brand-300">make linkedin-login</code> (or{' '}
              <code className="font-mono text-brand-300">scripts/linkedin-login.ps1</code> on Windows) on the host
              instead.
            </p>
          )}
          <p className="text-2xs text-ink-400">
            Credentials go straight to LinkedIn in that viewer. Hermes only keeps the resulting session cookie inside
            the linkedin-mcp container.
          </p>
        </div>
      </Modal>
    </div>
  );
}

/* -------------------------------------------------------------------------- */
/* Drawer body                                                                */
/* -------------------------------------------------------------------------- */

function JobDetail({
  job,
  breakdown,
  notesState,
  statusPending,
  tailoring,
  tailorDisabled,
  onStatus,
  onNotes,
  onSaveNotes,
  onTailor,
  onApplyClick,
}: {
  job: Job;
  breakdown: MatchBreakdown | null;
  notesState: 'dirty' | 'saving' | 'saved' | 'failed' | undefined;
  statusPending: boolean;
  tailoring: boolean;
  tailorDisabled: boolean;
  onStatus: (next: JobStatus) => void;
  onNotes: (text: string) => void;
  onSaveNotes: () => void;
  onTailor: () => void;
  onApplyClick: () => void;
}) {
  const href = applyUrlOf(job);
  const notesHint: Record<string, string> = {
    dirty: 'Unsaved…',
    saving: 'Saving…',
    saved: 'Saved',
    failed: 'Save failed',
  };

  return (
    <div className="flex flex-col gap-5">
      <div className="flex flex-wrap items-center gap-2">
        <ScoreBadge score={job.match_score} verdict={breakdown?.verdict} />
        {breakdown?.verdict ? <Badge tone="info">{breakdown.verdict}</Badge> : null}
        {job.easy_apply ? <Badge tone="brand">Easy Apply</Badge> : null}
        {job.posted ? <span className="text-2xs text-ink-300">Posted {job.posted}</span> : null}
        <span className="text-2xs text-ink-400">Discovered {fmtRelative(job.discovered_at)}</span>
      </div>

      <div className="flex flex-wrap items-center gap-2">
        {href ? (
          <a
            href={href}
            target="_blank"
            rel="noopener noreferrer"
            onClick={onApplyClick}
            className="inline-flex items-center gap-1 rounded-md bg-brand-500 px-3 py-1.5 text-xs font-semibold text-ink-950 hover:bg-brand-400"
          >
            Open &amp; Apply on LinkedIn <span aria-hidden>&#8599;</span>
          </a>
        ) : (
          <span className="text-xs text-ink-400">This posting has no usable http(s) link.</span>
        )}
        <Button variant="ghost" size="sm" disabled={tailoring || tailorDisabled} onClick={onTailor}>
          {tailoring ? 'Tailoring…' : 'Tailor resume for this job'}
        </Button>
        {job.tailored_resume_id ? (
          <Link
            to="/resume"
            className="text-2xs text-brand-300 underline-offset-2 hover:underline"
            title={`Resume #${job.tailored_resume_id} was tailored for this posting.`}
          >
            View tailored resume #{job.tailored_resume_id}
          </Link>
        ) : null}
      </div>

      <p className="text-2xs text-ink-400">
        You submit the application on LinkedIn. Hermes only tracks the outcome you record here.
      </p>

      <div className="grid gap-3 sm:grid-cols-2">
        <LabeledField label="Status">
          <StatusSelect value={statusOf(job)} disabled={statusPending} onChange={onStatus} className="w-full" />
        </LabeledField>
        <LabeledField label="LinkedIn job id">
          <input className={cx(CONTROL_CLASS, 'font-mono')} readOnly value={job.linkedin_job_id ?? '—'} />
        </LabeledField>
      </div>

      <div>
        <div className="mb-1 flex items-center justify-between">
          <span className="text-2xs font-medium uppercase tracking-wide text-ink-300">Notes</span>
          <span className="flex items-center gap-2 text-2xs text-ink-400">
            {notesState ? <span className={notesState === 'failed' ? 'text-bad-400' : ''}>{notesHint[notesState]}</span> : null}
            <button
              type="button"
              className="text-brand-300 underline-offset-2 hover:underline"
              onClick={onSaveNotes}
            >
              Save now
            </button>
          </span>
        </div>
        <textarea
          className={cx(CONTROL_CLASS, 'min-h-[5rem] resize-y')}
          value={job.notes ?? ''}
          placeholder="Recruiter name, referral, salary range, follow-up date…"
          onChange={(event) => onNotes(event.target.value)}
          onBlur={onSaveNotes}
        />
        <p className="mt-1 text-2xs text-ink-400">Saved automatically about a second after you stop typing.</p>
      </div>

      {breakdown ? (
        <div className="flex flex-col gap-4 rounded-lg border border-ink-700 bg-ink-875 p-3">
          <h3 className="text-xs font-semibold uppercase tracking-wide text-ink-200">Why this score</h3>
          <BulletList title="Reasons" items={breakdown.reasons} />
          <div>
            <h4 className="mb-1 text-2xs font-semibold uppercase tracking-wide text-ink-300">Your matching skills</h4>
            <KeywordChips items={breakdown.matched_skills ?? []} tone="good" emptyText="None detected." />
          </div>
          <div>
            <h4 className="mb-1 text-2xs font-semibold uppercase tracking-wide text-ink-300">Gaps in the posting</h4>
            <KeywordChips items={breakdown.missing_skills ?? []} tone="bad" emptyText="No gaps flagged." />
          </div>
          <BulletList title="Tailoring notes" items={breakdown.tailoring_notes} />
        </div>
      ) : (
        <p className="rounded-lg border border-ink-700 bg-ink-875 p-3 text-xs text-ink-300">
          This posting has not been scored. Import your LinkedIn profile on the Profile page, then re-run the search —
          ranking needs a profile analysis to compare against.
        </p>
      )}

      <div>
        <h3 className="mb-1 text-xs font-semibold uppercase tracking-wide text-ink-200">Job description</h3>
        {job.description ? (
          <div className="max-h-80 overflow-y-auto whitespace-pre-line rounded-lg border border-ink-700 bg-ink-875 p-3 text-xs leading-relaxed text-ink-200">
            {job.description}
          </div>
        ) : (
          <p className="text-xs text-ink-400">
            No description was scraped. Re-run the search with &ldquo;Fetch full descriptions&rdquo; enabled for better
            ranking and tailoring.
          </p>
        )}
      </div>

      {job.applied_at ? (
        <p className="text-2xs text-ink-400">You marked this applied {fmtRelative(job.applied_at)}.</p>
      ) : null}
    </div>
  );
}
