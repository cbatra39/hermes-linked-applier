/**
 * Resume — ATS scoring, version history, previews and downloads.
 *
 * Three things happen here:
 *   1. Generate a resume version from the imported profile (optionally aimed at
 *      one job), which renders md/docx/txt/pdf and scores the result.
 *   2. Upload an existing resume so Hermes has a baseline to work from.
 *   3. Inspect any version: the ATS breakdown, keyword coverage, and the
 *      rendered markdown, with download links for every format that exists.
 *
 * HONESTY CONTRACT: the "ATS score" is Hermes' own heuristic (agents/ats.py:
 * parseability, keyword coverage, contact block, experience quality, formatting,
 * readability) plus an optional LLM semantic pass. It is NOT a reading from
 * Workday / Greenhouse / Taleo / iCIMS or any other real ATS, and no vendor
 * publishes such a score. Every label here has to keep saying that.
 *
 * Shared-UI prop contract assumed from '../components':
 *   Badge        {tone?, title?, className?}
 *   Button       {variant?: 'primary'|'ghost'|'danger', size?: 'sm'|'md', className?, ...button}
 *   Card         {title?, subtitle?, actions?, className?, bodyClassName?}
 *   DataTable    {rows, columns:{key,header,render,align?,width?,className?}[], rowKey, onRowClick?, loading?, empty?, dense?}
 *   EmptyState   {title, message?, action?}
 *   ErrorState   {title?, message, onRetry?}
 *   KeywordChips {items: string[], tone?: 'good'|'bad'|'neutral', emptyText?}
 *   LogStream    {lines, connected?, failed?, onReconnect?, className?}
 *   MarkdownView {markdown: string, className?}
 *   ScoreGauge   {score: number|null, label?: string, caption?: string}
 *   Spinner      {}
 *   SubscoreBars {subscores: AtsSubscores, className?}
 */

import { useCallback, useEffect, useMemo, useRef, useState } from 'react';

import {
  Badge,
  Button,
  Card,
  DataTable,
  EmptyState,
  ErrorState,
  KeywordChips,
  LogStream,
  MarkdownView,
  ScoreGauge,
  Spinner,
  SubscoreBars,
} from '../components';
import { api, errorMessage, parseJsonish, resumeBreakdown } from '../lib/api';
import { cx, fmtDateTime, fmtNum, fmtRelative, scoreTone } from '../lib/format';
import { useApi, useRunWatch, useStream } from '../lib/hooks';
import type { AtsBreakdown, Id, Job, Resume, ResumeFormat } from '../lib/types';

/* -------------------------------------------------------------------------- */
/* Wire-shape adapters                                                        */
/* -------------------------------------------------------------------------- */

/**
 * hermes-core serialises Resume through schemas.ResumeOut, which flattens the
 * JSON column to `ats_breakdown` (an object) and adds `available_formats`.
 * lib/types.ts mirrors the DB row instead (`ats_breakdown_json`). Read both.
 */
type ResumeWire = Resume & {
  ats_breakdown?: unknown;
  available_formats?: unknown;
};

function atsOf(resume: Resume | null | undefined): AtsBreakdown | null {
  if (!resume) return null;
  return resumeBreakdown(resume) ?? parseJsonish<AtsBreakdown>((resume as ResumeWire).ats_breakdown);
}

const ALL_FORMATS: readonly ResumeFormat[] = ['md', 'docx', 'pdf', 'txt'] as const;

function isFormat(value: unknown): value is ResumeFormat {
  return value === 'md' || value === 'docx' || value === 'pdf' || value === 'txt';
}

/** Which download formats actually exist on disk for this version. */
function formatsOf(resume: Resume | null | undefined): Set<ResumeFormat> {
  const available = new Set<ResumeFormat>();
  if (!resume) return available;

  const wire = resume as ResumeWire;
  if (Array.isArray(wire.available_formats)) {
    for (const entry of wire.available_formats) {
      if (isFormat(entry)) available.add(entry);
    }
  }
  // Markdown lives in the DB row, so it is always downloadable.
  if (resume.markdown || available.size === 0) available.add('md');
  if (resume.docx_path) available.add('docx');
  if (resume.pdf_path) available.add('pdf');
  if (resume.txt_path) available.add('txt');
  return available;
}

const FORMAT_LABELS: Record<ResumeFormat, string> = {
  md: 'Markdown',
  docx: 'DOCX',
  pdf: 'PDF',
  txt: 'Plain text',
};

const MISSING_FORMAT_HINT: Record<ResumeFormat, string> = {
  md: 'This version has no stored markdown.',
  docx: 'No .docx was rendered for this version. Re-generate it to produce one.',
  pdf:
    'No PDF exists for this version. PDF rendering needs LibreOffice inside the sandbox image — ' +
    'download the DOCX and export a PDF yourself, or install LibreOffice and re-generate.',
  txt: 'No plain-text render exists for this version.',
};

const BADGE_TONE: Record<string, 'good' | 'warn' | 'bad' | 'neutral'> = {
  good: 'good',
  ok: 'warn',
  weak: 'bad',
  none: 'neutral',
};

function versionLabel(resume: Resume): string {
  const version = resume.version ?? null;
  const label = (resume.label ?? '').trim();
  if (label) return version ? `v${version} — ${label}` : label;
  return version ? `Version ${version}` : `Resume ${resume.id}`;
}

/* -------------------------------------------------------------------------- */
/* Local pieces                                                               */
/* -------------------------------------------------------------------------- */

const CONTROL_CLASS =
  'w-full rounded-md border border-ink-600 bg-ink-850 px-2.5 py-1.5 text-sm text-ink-100 ' +
  'placeholder:text-ink-400 focus:border-brand-400 focus:outline-none focus:ring-1 focus:ring-brand-400/40';

function AdviceList({
  title,
  items,
  tone,
}: {
  title: string;
  items: string[] | undefined;
  tone: 'bad' | 'brand';
}) {
  if (!items || items.length === 0) return null;
  return (
    <div>
      <h4 className="mb-1 text-2xs font-semibold uppercase tracking-wide text-ink-300">{title}</h4>
      <ul className="space-y-1.5 text-sm text-ink-200">
        {items.map((item, index) => (
          <li key={`${title}-${index}`} className="flex gap-2">
            <span
              aria-hidden
              className={cx(
                'mt-[0.4rem] h-1.5 w-1.5 shrink-0 rounded-full',
                tone === 'bad' ? 'bg-bad-400' : 'bg-brand-400',
              )}
            />
            <span>{item}</span>
          </li>
        ))}
      </ul>
    </div>
  );
}

function DownloadRow({ resume }: { resume: Resume }) {
  const available = formatsOf(resume);
  return (
    <div className="flex flex-wrap items-center gap-2">
      {ALL_FORMATS.map((fmt) => {
        const ok = available.has(fmt);
        if (!ok) {
          return (
            <span
              key={fmt}
              title={MISSING_FORMAT_HINT[fmt]}
              className="cursor-not-allowed rounded-md border border-dashed border-ink-600 px-2.5 py-1.5 text-2xs text-ink-500"
            >
              {FORMAT_LABELS[fmt]} — unavailable
            </span>
          );
        }
        return (
          <a
            key={fmt}
            href={api.resumeDownloadUrl(resume.id, fmt)}
            download
            className="rounded-md border border-ink-600 bg-ink-850 px-2.5 py-1.5 text-2xs font-medium text-ink-100 transition-colors hover:border-brand-400 hover:text-brand-200"
          >
            Download {FORMAT_LABELS[fmt]}
          </a>
        );
      })}
    </div>
  );
}

/* -------------------------------------------------------------------------- */
/* Page                                                                       */
/* -------------------------------------------------------------------------- */

type Notice = { tone: 'good' | 'bad'; text: string } | null;

interface ActiveRun {
  id: Id;
  label: string;
}

export default function ResumePage() {
  const resumesQuery = useApi((signal) => api.listResumes(signal), []);
  const jobsQuery = useApi((signal) => api.listJobs(undefined, signal), []);

  const resumes = useMemo(() => {
    const rows = (resumesQuery.data ?? []).slice();
    // Newest first: version desc, then created_at desc as a tiebreak.
    rows.sort((a, b) => {
      const byVersion = (b.version ?? 0) - (a.version ?? 0);
      if (byVersion !== 0) return byVersion;
      return String(b.created_at ?? '').localeCompare(String(a.created_at ?? ''));
    });
    return rows;
  }, [resumesQuery.data]);

  const jobs = jobsQuery.data ?? [];

  const [selectedId, setSelectedId] = useState<Id | null>(null);
  const [targetJobId, setTargetJobId] = useState<string>('');
  const [notice, setNotice] = useState<Notice>(null);
  const [generating, setGenerating] = useState(false);
  const [scoring, setScoring] = useState(false);
  const [uploading, setUploading] = useState(false);
  const [uploadSummary, setUploadSummary] = useState<string | null>(null);
  const [activeRun, setActiveRun] = useState<ActiveRun | null>(null);

  const fileInput = useRef<HTMLInputElement | null>(null);

  /* Pick the newest version once the list arrives (or after it changes). */
  useEffect(() => {
    if (resumes.length === 0) {
      setSelectedId(null);
      return;
    }
    setSelectedId((current) => {
      if (current !== null && resumes.some((resume) => String(resume.id) === String(current))) return current;
      return resumes[0].id;
    });
  }, [resumes]);

  /* Full detail (markdown + breakdown) for the selected version. */
  const detailQuery = useApi(
    (signal) => api.getResume(selectedId as Id, signal),
    [selectedId],
    { enabled: selectedId !== null },
  );

  // The list row is a usable fallback while the detail request is in flight.
  const listRow = useMemo(
    () => resumes.find((resume) => String(resume.id) === String(selectedId)) ?? null,
    [resumes, selectedId],
  );
  // useApi keeps the previous payload while a new id is in flight, so only trust
  // the detail response when it actually belongs to the selected version.
  const detail =
    detailQuery.data && String(detailQuery.data.id) === String(selectedId) ? detailQuery.data : null;
  const selected = detail ?? listRow;
  const ats = atsOf(selected);

  /* ---- live run -------------------------------------------------------- */
  const stream = useStream(activeRun ? api.runEventsPath(activeRun.id) : null, 400);
  const clearStream = stream.clear;
  const reloadResumes = resumesQuery.reload;
  const reloadDetail = detailQuery.reload;

  const { run: watchedRun } = useRunWatch(activeRun?.id ?? null, (finished) => {
    setGenerating(false);
    setScoring(false);
    reloadResumes();
    reloadDetail();
    if (finished.status === 'error') {
      setNotice({ tone: 'bad', text: finished.error || 'The run failed — see the log below.' });
    } else {
      setNotice({ tone: 'good', text: 'Run finished. The version list has been refreshed.' });
    }
  });

  /* ---- actions --------------------------------------------------------- */
  const generate = useCallback(async () => {
    setGenerating(true);
    setNotice(null);
    clearStream();
    try {
      const run = await api.generateResume(targetJobId ? { target_job_id: targetJobId } : {});
      const job = jobs.find((entry) => String(entry.id) === targetJobId);
      setActiveRun({
        id: run.id,
        label: job ? `Building resume for ${job.title ?? 'job'} @ ${job.company ?? '?'}` : 'Building resume',
      });
    } catch (err) {
      setGenerating(false);
      setNotice({ tone: 'bad', text: `Generation could not start: ${errorMessage(err)}` });
    }
  }, [clearStream, jobs, targetJobId]);

  const rescore = useCallback(async () => {
    if (selectedId === null) return;
    setScoring(true);
    setNotice(null);
    clearStream();
    try {
      const run = await api.scoreResume(
        targetJobId ? { resume_id: selectedId, job_id: targetJobId } : { resume_id: selectedId },
      );
      setActiveRun({ id: run.id, label: `Re-scoring resume ${selectedId}` });
    } catch (err) {
      setScoring(false);
      setNotice({ tone: 'bad', text: `Scoring could not start: ${errorMessage(err)}` });
    }
  }, [clearStream, selectedId, targetJobId]);

  const upload = useCallback(
    async (file: File) => {
      setUploading(true);
      setNotice(null);
      setUploadSummary(null);
      try {
        const response = await api.uploadResume(file);
        const chars = typeof response.chars === 'number' ? response.chars : null;
        const detected = typeof response.format === 'string' ? response.format : file.name.split('.').pop();
        setUploadSummary(
          `Stored "${file.name}"${detected ? ` as ${String(detected).toUpperCase()}` : ''}` +
            `${chars !== null ? `, ${chars.toLocaleString()} characters of text extracted` : ''}. ` +
            'Uploads are stored as text and are not scored automatically — use "Re-score" for an ATS reading.',
        );
        setNotice({ tone: 'good', text: 'Resume uploaded.' });
        reloadResumes();
      } catch (err) {
        setNotice({ tone: 'bad', text: `Upload failed: ${errorMessage(err)}` });
      } finally {
        setUploading(false);
        if (fileInput.current) fileInput.current.value = '';
      }
    },
    [reloadResumes],
  );

  /* ---- version table --------------------------------------------------- */
  const columns = useMemo(
    () => [
      {
        key: 'version',
        header: 'Version',
        render: (resume: Resume) => (
          <div className="min-w-0">
            <div className="truncate text-sm font-medium text-ink-100">{versionLabel(resume)}</div>
            <div className="text-2xs text-ink-400" title={fmtDateTime(resume.created_at)}>
              {resume.created_at ? fmtRelative(resume.created_at) : 'no timestamp'}
            </div>
          </div>
        ),
      },
      {
        key: 'target',
        header: 'Target job',
        width: '110px',
        render: (resume: Resume) =>
          resume.target_job_id ? (
            <Badge tone="info" title={`Tailored for job ${resume.target_job_id}`}>
              Job {resume.target_job_id}
            </Badge>
          ) : (
            <span className="text-2xs text-ink-400">General</span>
          ),
      },
      {
        key: 'score',
        header: 'ATS',
        width: '68px',
        align: 'center' as const,
        render: (resume: Resume) => {
          const score = resume.ats_score;
          const has = score !== null && score !== undefined;
          return (
            <Badge
              tone={BADGE_TONE[scoreTone(score)] ?? 'neutral'}
              className="min-w-[2.75rem] justify-center font-mono tabular-nums"
              title="Hermes heuristic ATS proxy, 0-100. Not a real ATS vendor score."
            >
              {has ? fmtNum(score, 0) : '—'}
            </Badge>
          );
        },
      },
    ],
    [],
  );

  /* ---- render ---------------------------------------------------------- */
  const semanticFit =
    ats?.semantic_fit === null || ats?.semantic_fit === undefined ? null : Number(ats.semantic_fit);

  return (
    <div className="flex flex-col gap-4">
      {/* heuristic disclaimer — required by the product contract */}
      <div className="flex items-start gap-2.5 rounded-lg border border-brand-800/70 bg-brand-800/15 px-3.5 py-2.5">
        <span aria-hidden className="mt-0.5 text-brand-300">
          &#9432;
        </span>
        <p className="text-xs leading-relaxed text-ink-200">
          <strong className="font-semibold text-brand-200">The ATS score is Hermes&apos; own heuristic proxy.</strong>{' '}
          It measures parseability, keyword coverage, a machine-readable contact block, quantified experience,
          formatting and readability — the things applicant tracking systems actually choke on. No real ATS (Workday,
          Greenhouse, Taleo, iCIMS…) publishes a score, so treat this as a checklist, not a verdict.
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

      {/* ---- build / upload --------------------------------------------- */}
      <Card
        title="Build a resume version"
        subtitle="Generation reads your imported profile, writes ATS-safe markdown, renders it, and scores the result."
      >
        <div className="grid gap-4 lg:grid-cols-2">
          <div className="flex flex-col gap-3">
            <label className="flex flex-col gap-1">
              <span className="text-2xs font-medium uppercase tracking-wide text-ink-300">
                Target job (optional)
              </span>
              <select
                className={CONTROL_CLASS}
                value={targetJobId}
                onChange={(event) => setTargetJobId(event.target.value)}
              >
                <option value="">General resume — no specific posting</option>
                {jobs.map((job: Job) => (
                  <option key={String(job.id)} value={String(job.id)}>
                    {job.match_score !== null && job.match_score !== undefined
                      ? `[${fmtNum(job.match_score, 0)}] `
                      : ''}
                    {job.title ?? 'Untitled'} — {job.company ?? 'unknown company'}
                  </option>
                ))}
              </select>
              <span className="text-2xs text-ink-400">
                {jobsQuery.error
                  ? `Job list unavailable: ${jobsQuery.error}`
                  : jobs.length === 0
                    ? 'No jobs stored yet — run a search on the Jobs page to tailor against a real posting.'
                    : 'Picking a job tailors the wording and scores the result against that job description.'}
              </span>
            </label>

            <div className="flex flex-wrap items-center gap-2">
              <Button variant="primary" disabled={generating || activeRun !== null} onClick={() => void generate()}>
                {generating ? 'Generating…' : 'Generate resume'}
              </Button>
              <Button
                variant="ghost"
                disabled={scoring || selectedId === null || activeRun !== null}
                onClick={() => void rescore()}
                title="Re-run the ATS heuristic (plus the LLM semantic pass) on the selected version."
              >
                {scoring ? 'Scoring…' : 'Re-score selected'}
              </Button>
              {generating || scoring ? <Spinner /> : null}
            </div>
          </div>

          <div className="flex flex-col gap-2 rounded-lg border border-ink-700 bg-ink-875 p-3">
            <h3 className="text-xs font-semibold uppercase tracking-wide text-ink-200">Upload an existing resume</h3>
            <p className="text-2xs text-ink-400">
              Hermes extracts the text and keeps it as a baseline. Markdown, plain text, DOCX and PDF are accepted.
            </p>
            <input
              ref={fileInput}
              type="file"
              accept=".md,.markdown,.txt,.docx,.pdf"
              disabled={uploading}
              className="block w-full cursor-pointer rounded-md border border-ink-600 bg-ink-850 p-2 text-2xs text-ink-200 file:mr-3 file:rounded file:border-0 file:bg-brand-500 file:px-2.5 file:py-1 file:text-2xs file:font-semibold file:text-ink-950 hover:file:bg-brand-400"
              onChange={(event) => {
                const file = event.target.files?.[0];
                if (file) void upload(file);
              }}
            />
            {uploading ? (
              <span className="flex items-center gap-2 text-2xs text-ink-300">
                <Spinner /> Uploading…
              </span>
            ) : null}
            {uploadSummary ? <p className="text-2xs text-good-400">{uploadSummary}</p> : null}
          </div>
        </div>
      </Card>

      {/* ---- live run ---------------------------------------------------- */}
      {activeRun ? (
        <Card
          title={activeRun.label}
          subtitle={watchedRun?.status ? `Run ${activeRun.id} — ${watchedRun.status}` : `Run ${activeRun.id} — starting…`}
          actions={
            <Button variant="ghost" size="sm" onClick={() => setActiveRun(null)}>
              Close
            </Button>
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

      {/* ---- versions + detail ------------------------------------------ */}
      <div className="grid gap-4 xl:grid-cols-[minmax(0,20rem)_minmax(0,1fr)]">
        <Card
          title="Versions"
          subtitle={`${resumes.length} stored`}
          actions={
            <div className="flex items-center gap-2">
              {resumesQuery.refreshing ? <Spinner /> : null}
              <Button variant="ghost" size="sm" onClick={() => resumesQuery.reload()}>
                Refresh
              </Button>
            </div>
          }
        >
          {resumesQuery.error ? (
            <ErrorState
              title="Could not load resumes"
              message={resumesQuery.error}
              onRetry={() => resumesQuery.reload()}
            />
          ) : (
            <DataTable
              rows={resumes}
              columns={columns}
              rowKey={(resume: Resume) => String(resume.id)}
              loading={resumesQuery.loading}
              onRowClick={(resume: Resume) => setSelectedId(resume.id)}
              dense
              empty={
                <EmptyState
                  title="No resume versions yet"
                  message="Generate one from your imported profile, or upload the resume you already use."
                />
              }
            />
          )}
        </Card>

        <div className="flex flex-col gap-4">
          {selected === null ? (
            <Card title="Resume detail">
              <EmptyState
                title="Nothing selected"
                message="Pick a version on the left, or generate your first one above."
              />
            </Card>
          ) : (
            <>
              <Card
                title={versionLabel(selected)}
                subtitle={
                  selected.created_at
                    ? `Created ${fmtDateTime(selected.created_at)}`
                    : 'No creation timestamp recorded'
                }
                actions={detailQuery.refreshing ? <Spinner /> : null}
              >
                {detailQuery.error ? (
                  <div className="mb-3">
                    <ErrorState
                      title="Could not load the full version"
                      message={`${detailQuery.error} — showing the summary from the list instead.`}
                      onRetry={() => detailQuery.reload()}
                    />
                  </div>
                ) : null}

                <div className="flex flex-col gap-5">
                  <div className="grid gap-5 md:grid-cols-[auto_minmax(0,1fr)] md:items-center">
                    <ScoreGauge
                      score={selected.ats_score ?? ats?.score ?? null}
                      label="ATS proxy"
                      caption="Hermes heuristic, 0-100"
                    />
                    <div className="flex flex-col gap-3">
                      {ats?.subscores ? (
                        <SubscoreBars subscores={ats.subscores} />
                      ) : (
                        <p className="text-xs text-ink-400">
                          No subscore breakdown stored for this version. Run &ldquo;Re-score selected&rdquo; to
                          compute one.
                        </p>
                      )}
                      {semanticFit !== null && !Number.isNaN(semanticFit) ? (
                        <p className="text-2xs text-ink-300">
                          LLM semantic fit:{' '}
                          <span className="font-mono tabular-nums text-brand-200">{fmtNum(semanticFit, 0)}</span>
                          /100 — a language-model opinion on how well the wording matches the target, separate from
                          the mechanical checks above.
                        </p>
                      ) : null}
                    </div>
                  </div>

                  <div className="grid gap-4 md:grid-cols-2">
                    <div>
                      <h4 className="mb-1 text-2xs font-semibold uppercase tracking-wide text-ink-300">
                        Keywords covered
                      </h4>
                      <KeywordChips
                        items={ats?.matched ?? []}
                        tone="good"
                        emptyText="No keyword matches recorded."
                      />
                    </div>
                    <div>
                      <h4 className="mb-1 text-2xs font-semibold uppercase tracking-wide text-ink-300">
                        Keywords missing
                      </h4>
                      <KeywordChips
                        items={ats?.missing ?? []}
                        tone="bad"
                        emptyText="Nothing flagged as missing."
                      />
                    </div>
                  </div>

                  {ats ? (
                    <div className="grid gap-4 md:grid-cols-2">
                      <div className="flex flex-col gap-4">
                        <AdviceList title="Issues found" items={ats.issues} tone="bad" />
                        <AdviceList title="Issues raised by the model" items={ats.llm_issues} tone="bad" />
                      </div>
                      <div className="flex flex-col gap-4">
                        <AdviceList title="Advice" items={ats.advice} tone="brand" />
                        <AdviceList title="Advice from the model" items={ats.llm_advice} tone="brand" />
                      </div>
                    </div>
                  ) : null}

                  <div>
                    <h4 className="mb-1.5 text-2xs font-semibold uppercase tracking-wide text-ink-300">Downloads</h4>
                    <DownloadRow resume={selected} />
                    <p className="mt-1.5 text-2xs text-ink-400">
                      DOCX is the safest format to submit: single column, no tables, no text boxes, standard section
                      headings.
                    </p>
                  </div>
                </div>
              </Card>

              <Card
                title="Preview"
                subtitle="The markdown Hermes rendered every format from."
                bodyClassName="max-h-[36rem] overflow-y-auto"
              >
                {selected.markdown ? (
                  <MarkdownView markdown={selected.markdown} />
                ) : detailQuery.loading ? (
                  <span className="flex items-center gap-2 text-xs text-ink-300">
                    <Spinner /> Loading markdown…
                  </span>
                ) : (
                  <EmptyState
                    title="No markdown stored"
                    message="This version has no markdown body — regenerate it to get a previewable resume."
                  />
                )}
              </Card>
            </>
          )}
        </div>
      </div>
    </div>
  );
}
