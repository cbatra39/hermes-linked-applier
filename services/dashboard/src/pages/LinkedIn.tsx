/**
 * LinkedIn — session status, the manual sign-in walkthrough, and profile import.
 *
 * DESIGN CONSTRAINT, stated plainly on the page itself: Hermes does not and will
 * not automate the LinkedIn sign-in. The user opens a noVNC viewer onto a
 * throwaway container and types their own password, 2FA code and captcha there.
 * The authenticated browser profile is left in the `linkedin-session` Docker
 * volume, which the long-running linkedin-mcp service reads on its next start.
 *
 * Once a session exists, "Import my profile" kicks off the profile_import
 * pipeline and this page tails its run events live.
 */

import { useMemo, useState } from 'react';
import { Link } from 'react-router-dom';

import {
  Badge,
  Button,
  Card,
  EmptyState,
  ErrorState,
  KeywordChips,
  LogStream,
  Spinner,
  StatusDot,
} from '../components';
import { api } from '../lib/api';
import { cx, fmtDateTime, fmtDuration, fmtNum } from '../lib/format';
import { useAction, useApi, useRunWatch, useStream } from '../lib/hooks';
import type { Id, LinkedInLoginResponse, McpHealth, ProfileAnalysis } from '../lib/types';

/* -------------------------------------------------------------------------- */
/* Constants                                                                   */
/* -------------------------------------------------------------------------- */

const STATUS_POLL_MS = 10_000;

/** Compose publishes the noVNC desktop here (LINKEDIN_VIEWER_PORT, default 6080). */
const DEFAULT_VIEWER_URL = 'http://127.0.0.1:6080/vnc.html';

/**
 * The exact text linkedin-mcp returns when the session volume holds no cookies.
 * Matching on it lets us swap a raw tool error for the sign-in walkthrough.
 */
const NO_SESSION_MARKER = 'no valid linkedin session';

type Tone = 'good' | 'warn' | 'bad' | 'neutral' | 'info';

const LINK_CLS =
  'inline-flex items-center gap-1 rounded-md border border-ink-700 bg-ink-800 px-2.5 py-1 text-xs ' +
  'font-medium text-ink-100 transition-colors hover:border-brand-600 hover:text-brand-200 ' +
  'focus:outline-none focus-visible:ring-2 focus-visible:ring-brand-500';

const CODE_CLS = 'rounded bg-ink-950 px-1.5 py-0.5 font-mono text-2xs text-brand-300';

/* -------------------------------------------------------------------------- */
/* Helpers                                                                     */
/* -------------------------------------------------------------------------- */

function pick<T>(source: unknown, key: string): T | undefined {
  if (source && typeof source === 'object' && key in source) {
    return (source as Record<string, unknown>)[key] as T;
  }
  return undefined;
}

interface SessionView {
  tone: Tone;
  headline: string;
  detail: string;
  connected: boolean;
}

function describeSession(status: McpHealth | null, error: string | null): SessionView {
  if (error) {
    return {
      tone: 'bad',
      headline: 'Status unavailable',
      detail: error,
      connected: false,
    };
  }
  if (!status) {
    return { tone: 'neutral', headline: 'Checking…', detail: '', connected: false };
  }
  const detail = (status.detail ?? '').trim();
  if (status.authenticated) {
    return {
      tone: 'good',
      headline: 'LinkedIn connected',
      detail: detail || 'linkedin-mcp is holding a valid session.',
      connected: true,
    };
  }
  if (status.reachable) {
    const noSession = detail.toLowerCase().includes(NO_SESSION_MARKER);
    return {
      tone: 'warn',
      headline: 'LinkedIn not connected',
      detail: noSession
        ? 'linkedin-mcp is running but the session volume is empty. Sign in once, by hand, using the steps below.'
        : detail || 'linkedin-mcp is running but reports no authenticated session.',
      connected: false,
    };
  }
  return {
    tone: 'bad',
    headline: 'linkedin-mcp unreachable',
    detail:
      detail ||
      'Nothing is answering at LINKEDIN_MCP_URL. Start the stack with `make up` and check `make logs`.',
    connected: false,
  };
}

/** Slug or full URL in, vanity slug out (matches ProfileImportRequest's validator). */
function cleanUsername(raw: string): string {
  const value = raw.trim();
  if (!value) return '';
  const match = value.match(/linkedin\.com\/in\/([^/?#]+)/i);
  const slug = match ? match[1] : value;
  return slug.replace(/^\/+|\/+$/g, '').trim();
}

/* -------------------------------------------------------------------------- */
/* Sign-in walkthrough                                                         */
/* -------------------------------------------------------------------------- */

function SignInSteps({
  viewerUrl,
  onOpenViewer,
  opening,
  openError,
  login,
}: {
  viewerUrl: string;
  onOpenViewer: () => void;
  opening: boolean;
  openError: string | null;
  login: LinkedInLoginResponse | null;
}) {
  return (
    <Card
      title="Sign in to LinkedIn by hand"
      subtitle="One time, in a browser you control"
      actions={<Badge tone="warn">action required</Badge>}
    >
      <div className="rounded-md border border-ink-700 bg-ink-850 p-3 text-xs leading-relaxed text-ink-200">
        <p className="font-semibold text-ink-100">Hermes cannot log in for you.</p>
        <p className="mt-1">
          There is no automated sign-in and no captcha solver — deliberately. Hermes never sees, asks
          for, or stores your LinkedIn password: you type it straight into a Chromium window running
          inside a one-shot container. What Hermes reuses afterwards is the browser profile that
          Chromium leaves in the <code className={CODE_CLS}>linkedin-session</code> Docker volume, so
          you only do this again when those cookies expire.
        </p>
      </div>

      <ol className="mt-4 space-y-4">
        <li className="flex gap-3">
          <StepNumber n={1} />
          <div className="min-w-0 flex-1">
            <p className="text-sm font-medium text-ink-100">Start the login container</p>
            <p className="mt-1 text-xs leading-relaxed text-ink-400">
              From the project root, in a terminal:
            </p>
            <pre className="mt-1.5 overflow-x-auto rounded-md border border-ink-700 bg-ink-950 p-2.5 font-mono text-2xs leading-relaxed text-ink-100">
              {`make login-linkedin

# no make on Windows:
powershell -ExecutionPolicy Bypass -File scripts\\linkedin-login.ps1

# macOS / Linux without make:
./scripts/linkedin-login.sh`}
            </pre>
            <p className="mt-1.5 text-2xs leading-relaxed text-ink-400">
              Chromium takes an exclusive lock on the profile directory, so the helper stops
              linkedin-mcp first and brings it back afterwards. The scripts do that restart for you;
              after the bare <code className={CODE_CLS}>make login-linkedin</code> target you must run{' '}
              <code className={CODE_CLS}>make restart</code> yourself.
            </p>
          </div>
        </li>

        <li className="flex gap-3">
          <StepNumber n={2} />
          <div className="min-w-0 flex-1">
            <p className="text-sm font-medium text-ink-100">Open the noVNC viewer</p>
            <p className="mt-1 text-xs leading-relaxed text-ink-400">
              The container publishes a full desktop over noVNC. Open it once the terminal says the
              viewer is up:
            </p>
            <div className="mt-1.5 flex flex-wrap items-center gap-2">
              <a href={viewerUrl} target="_blank" rel="noopener noreferrer" className={LINK_CLS}>
                Open {viewerUrl}
              </a>
              <Button size="sm" variant="secondary" onClick={onOpenViewer} busy={opening} disabled={opening}>
                {opening ? 'Asking hermes-core…' : 'Ask hermes-core for the viewer URL'}
              </Button>
            </div>
            {openError && <p className="mt-2 text-2xs text-bad-400">{openError}</p>}
            {login && (
              <div className="mt-2 rounded-md border border-brand-700/50 bg-brand-800/20 p-2.5 text-2xs leading-relaxed text-ink-100">
                {login.viewer_url && (
                  <p>
                    Viewer:{' '}
                    <a
                      href={login.viewer_url}
                      target="_blank"
                      rel="noopener noreferrer"
                      className="font-mono text-brand-300 underline decoration-dotted"
                    >
                      {login.viewer_url}
                    </a>
                  </p>
                )}
                {login.instructions && (
                  <p className="mt-1 whitespace-pre-wrap text-ink-200">{login.instructions}</p>
                )}
              </div>
            )}
          </div>
        </li>

        <li className="flex gap-3">
          <StepNumber n={3} />
          <div className="min-w-0 flex-1">
            <p className="text-sm font-medium text-ink-100">Sign in with your own hands</p>
            <p className="mt-1 text-xs leading-relaxed text-ink-400">
              In that browser window: email, password, then whatever LinkedIn asks for — an SMS or
              authenticator 2FA code, an email verification code, a puzzle or captcha. Finish the flow
              until your normal LinkedIn feed loads. Nothing on this page can do that part.
            </p>
          </div>
        </li>

        <li className="flex gap-3">
          <StepNumber n={4} />
          <div className="min-w-0 flex-1">
            <p className="text-sm font-medium text-ink-100">Close the viewer and re-check</p>
            <p className="mt-1 text-xs leading-relaxed text-ink-400">
              Press <code className={CODE_CLS}>Ctrl-C</code> in the terminal to stop the login
              container. Cookies persist in the <code className={CODE_CLS}>linkedin-session</code>{' '}
              volume. Once linkedin-mcp is back up, hit <em>Re-check</em> at the top of this page —
              status refreshes on its own every {STATUS_POLL_MS / 1000}s too.
            </p>
          </div>
        </li>
      </ol>
    </Card>
  );
}

function StepNumber({ n }: { n: number }) {
  return (
    <span
      aria-hidden="true"
      className="mt-0.5 flex h-6 w-6 shrink-0 items-center justify-center rounded-full border border-brand-700 bg-brand-800/30 text-2xs font-bold text-brand-300"
    >
      {n}
    </span>
  );
}

/* -------------------------------------------------------------------------- */
/* Profile analysis                                                            */
/* -------------------------------------------------------------------------- */

function AnalysisSection({
  label,
  items,
  tone,
}: {
  label: string;
  items: string[] | undefined;
  tone?: 'brand' | 'neutral' | 'good' | 'warn';
}) {
  if (!items || items.length === 0) return null;
  return (
    <div>
      <p className="mb-1.5 text-2xs font-semibold uppercase tracking-wider text-ink-400">
        {label} <span className="tabular-nums opacity-70">({items.length})</span>
      </p>
      <KeywordChips items={items} tone={tone} />
    </div>
  );
}

function ProfileAnalysisView({ analysis }: { analysis: ProfileAnalysis }) {
  const years =
    typeof analysis.years_experience === 'number' ? fmtNum(analysis.years_experience, 1) : null;

  return (
    <div className="space-y-5">
      {(analysis.headline || analysis.seniority || years) && (
        <div>
          {analysis.headline && (
            <p className="text-base font-semibold leading-snug text-ink-100">{analysis.headline}</p>
          )}
          <div className="mt-1.5 flex flex-wrap items-center gap-2">
            {analysis.seniority && <Badge tone="brand">{analysis.seniority}</Badge>}
            {years && <Badge tone="neutral">{years} yrs experience</Badge>}
          </div>
        </div>
      )}

      {analysis.positioning_statement && (
        <blockquote className="border-l-2 border-brand-600 pl-3 text-sm italic leading-relaxed text-ink-200">
          {analysis.positioning_statement}
        </blockquote>
      )}

      {analysis.summary && (
        <p className="whitespace-pre-wrap text-sm leading-relaxed text-ink-200">{analysis.summary}</p>
      )}

      <div className="grid grid-cols-1 gap-4 sm:grid-cols-2">
        <AnalysisSection label="Target roles" items={analysis.target_roles} tone="brand" />
        <AnalysisSection label="Domains" items={analysis.domains} tone="neutral" />
        <AnalysisSection label="Hard skills" items={analysis.hard_skills} tone="good" />
        <AnalysisSection label="Tools" items={analysis.tools} tone="neutral" />
        <AnalysisSection label="Soft skills" items={analysis.soft_skills} tone="neutral" />
        <AnalysisSection label="Certifications" items={analysis.certifications} tone="brand" />
      </div>

      {analysis.achievements && analysis.achievements.length > 0 && (
        <div>
          <p className="mb-1.5 text-2xs font-semibold uppercase tracking-wider text-ink-400">
            Achievements <span className="tabular-nums opacity-70">({analysis.achievements.length})</span>
          </p>
          <ul className="space-y-1.5">
            {analysis.achievements.map((achievement, index) => (
              <li key={`${index}-${achievement.text.slice(0, 24)}`} className="flex gap-2 text-sm text-ink-200">
                <span aria-hidden="true" className="mt-1.5 h-1 w-1 shrink-0 rounded-full bg-brand-400" />
                <span className="leading-relaxed">
                  {achievement.text}
                  {achievement.metric && (
                    <span className="ml-1.5 font-mono text-2xs text-good-400">{achievement.metric}</span>
                  )}
                </span>
              </li>
            ))}
          </ul>
        </div>
      )}

      <AnalysisSection label="Keyword bank" items={analysis.keyword_bank} tone="neutral" />

      {analysis.gaps && analysis.gaps.length > 0 && (
        <div className="rounded-md border border-warn-600/40 bg-warn-500/5 p-3">
          <p className="mb-1.5 text-2xs font-semibold uppercase tracking-wider text-warn-400">
            Gaps to close
          </p>
          <ul className="space-y-1">
            {analysis.gaps.map((gap, index) => (
              <li key={`${index}-${gap.slice(0, 24)}`} className="text-xs leading-relaxed text-ink-200">
                • {gap}
              </li>
            ))}
          </ul>
        </div>
      )}
    </div>
  );
}

/* -------------------------------------------------------------------------- */
/* Page                                                                        */
/* -------------------------------------------------------------------------- */

export default function LinkedInPage() {
  const [username, setUsername] = useState('');
  const [importRunId, setImportRunId] = useState<Id | null>(null);
  const [login, setLogin] = useState<LinkedInLoginResponse | null>(null);

  const status = useApi<McpHealth>((signal) => api.linkedinStatus(signal), [], {
    intervalMs: STATUS_POLL_MS,
  });
  const profile = useApi((signal) => api.getProfile(signal), []);

  const reloadProfile = profile.reload;
  const reloadStatus = status.reload;

  const loginAction = useAction(async () => {
    const response = await api.linkedinLogin();
    setLogin(response);
    return response;
  });

  const importAction = useAction(async (slug: string) => {
    const run = await api.importProfile(slug || undefined);
    setImportRunId(run.id);
    return run;
  });

  const importStream = useStream(importRunId !== null ? api.runEventsPath(importRunId) : null, 800);
  const watch = useRunWatch(importRunId, () => {
    reloadProfile();
    reloadStatus();
  });

  const session = useMemo(() => describeSession(status.data, status.error), [status.data, status.error]);
  const mcpUrl = pick<string>(status.data, 'url') ?? '';
  const viewerUrl = (login?.viewer_url ?? '').trim() || DEFAULT_VIEWER_URL;

  const analysis = profile.data?.analysis ?? null;
  const profileRow = profile.data?.profile ?? null;

  const importing = importAction.busy || (importRunId !== null && !watch.done);
  const runStatus = String(watch.run?.status ?? (importRunId !== null ? 'pending' : ''));

  const startImport = () => {
    importStream.clear();
    setImportRunId(null);
    void importAction.run(cleanUsername(username));
  };

  return (
    <div className="space-y-6">
      {/* ------------------------------------------------------------ header */}
      <header className="flex flex-wrap items-end justify-between gap-3">
        <div>
          <h1 className="text-xl font-semibold tracking-tight text-ink-100">LinkedIn</h1>
          <p className="mt-1 text-sm text-ink-400">
            The scraper session and your imported profile. Hermes reads LinkedIn; it never posts,
            messages, or applies on your behalf.
          </p>
        </div>
        <div className="flex items-center gap-2">
          {status.refreshing && <Spinner size="sm" label="Checking" />}
          <Button variant="ghost" onClick={() => status.reload()} disabled={status.refreshing}>
            Re-check
          </Button>
        </div>
      </header>

      {/* ----------------------------------------------------- session status */}
      <Card>
        <div className="flex flex-wrap items-start justify-between gap-4">
          <div className="min-w-0 flex-1">
            <div className="flex items-center gap-2">
              <StatusDot tone={session.tone} pulse={status.loading} />
              <span className="text-2xs font-semibold uppercase tracking-wider text-ink-400">
                Session status
              </span>
            </div>
            <p className="mt-2 text-lg font-semibold text-ink-100">
              {status.loading ? <Spinner size="sm" label="Checking session" /> : session.headline}
            </p>
            {session.detail && (
              <p className="mt-1 max-w-2xl text-xs leading-relaxed text-ink-300">{session.detail}</p>
            )}
            {mcpUrl && (
              <p className="mt-2 font-mono text-2xs text-ink-400">
                linkedin-mcp: {mcpUrl}
                {status.data?.reachable ? ' · reachable' : ' · not answering'}
              </p>
            )}
          </div>
          <div className="flex shrink-0 flex-col items-end gap-2">
            <Badge tone={session.tone}>{session.connected ? 'authenticated' : 'no session'}</Badge>
            <Link to="/settings" className={LINK_CLS}>
              MCP settings
            </Link>
          </div>
        </div>
        {status.error && (
          <div className="mt-3 border-t border-ink-800 pt-3">
            <ErrorState message={status.error} onRetry={() => status.reload()} />
          </div>
        )}
      </Card>

      {/* ------------------------------------------------- sign-in walkthrough */}
      {!session.connected && (
        <SignInSteps
          viewerUrl={viewerUrl}
          onOpenViewer={() => void loginAction.run()}
          opening={loginAction.busy}
          openError={loginAction.error}
          login={login}
        />
      )}

      {/* ------------------------------------------------------ profile import */}
      <Card
        title="Import my profile"
        subtitle="Runs the profile_import pipeline and analyses the result"
        actions={
          runStatus ? (
            <Badge
              tone={
                runStatus === 'done'
                  ? 'good'
                  : runStatus === 'error'
                    ? 'bad'
                    : runStatus === 'running'
                      ? 'info'
                      : 'warn'
              }
            >
              {runStatus}
            </Badge>
          ) : null
        }
      >
        <div className="flex flex-wrap items-end gap-3">
          <div className="min-w-[16rem] flex-1">
            <label htmlFor="li-username" className="mb-1.5 block text-xs font-medium text-ink-300">
              LinkedIn username <span className="text-ink-400">(optional)</span>
            </label>
            <input
              id="li-username"
              type="text"
              value={username}
              onChange={(event) => setUsername(event.target.value)}
              placeholder="leave blank to scrape the signed-in account"
              autoComplete="off"
              spellCheck={false}
              className="block w-full rounded-md border border-ink-700 bg-ink-950 px-3 py-2 text-sm text-ink-100 placeholder:text-ink-500 focus:border-brand-600 focus:outline-none focus:ring-1 focus:ring-brand-600"
            />
            <p className="mt-1 text-2xs text-ink-400">
              A full profile URL works too — only the vanity slug is sent.
            </p>
          </div>
          <Button onClick={startImport} busy={importing} disabled={importing || !session.connected}>
            {importing ? 'Importing…' : 'Import my profile'}
          </Button>
        </div>

        {!session.connected && (
          <p className="mt-2 text-2xs text-warn-400">
            Needs a LinkedIn session — finish the sign-in steps above first.
          </p>
        )}

        {importAction.error && (
          <div className="mt-3">
            <ErrorState title="Import could not start" message={importAction.error} onRetry={startImport} />
          </div>
        )}

        {importRunId !== null && (
          <div className="mt-4 space-y-2 border-t border-ink-800 pt-4">
            <div className="flex flex-wrap items-center justify-between gap-2">
              <p className="text-xs text-ink-300">
                Run <span className="font-mono text-ink-100">#{String(importRunId)}</span>
                {watch.run?.started_at && ` · started ${fmtDateTime(watch.run.started_at)}`}
                {watch.run?.started_at && ` · ${fmtDuration(watch.run.started_at, watch.run.finished_at)}`}
              </p>
              <Link to={`/runs/${encodeURIComponent(String(importRunId))}`} className={LINK_CLS}>
                Full run
              </Link>
            </div>
            {watch.run?.error && (
              <p className="rounded-md border border-bad-600/40 bg-bad-500/10 p-2 text-xs leading-relaxed text-bad-400">
                {watch.run.error}
              </p>
            )}
            <div className="h-64">
              <LogStream
                lines={importStream.lines}
                connected={importStream.connected}
                failed={importStream.failed}
                onClear={importStream.clear}
                onReconnect={importStream.reconnect}
                emptyText="Waiting for the first event…"
                className="h-full"
              />
            </div>
          </div>
        )}
      </Card>

      {/* --------------------------------------------------- profile analysis */}
      <Card
        title="Profile analysis"
        subtitle={
          profileRow?.fetched_at
            ? `Imported ${fmtDateTime(profileRow.fetched_at)}`
            : 'What the analyst agent extracted'
        }
        actions={
          profileRow?.linkedin_username ? (
            <a
              href={`https://www.linkedin.com/in/${encodeURIComponent(profileRow.linkedin_username)}/`}
              target="_blank"
              rel="noopener noreferrer"
              className={LINK_CLS}
            >
              View on LinkedIn
            </a>
          ) : null
        }
      >
        {profile.error ? (
          <ErrorState message={profile.error} onRetry={() => profile.reload()} />
        ) : profile.loading ? (
          <Spinner label="Loading profile" />
        ) : !profileRow ? (
          <EmptyState
            title="No profile imported yet"
            description="Import your LinkedIn profile above. Every downstream agent — the resume writer, the match ranker, the ATS scorer — reads from this analysis."
          />
        ) : !analysis ? (
          <div className="space-y-3">
            <div className="rounded-md border border-warn-600/40 bg-warn-500/5 p-3 text-xs leading-relaxed text-ink-200">
              The profile row exists but has no analysis attached. That usually means the LLM router
              was unreachable during the import. Check the{' '}
              <Link to="/settings" className="text-brand-300 underline decoration-dotted">
                LLM settings
              </Link>{' '}
              and import again.
            </div>
            {profileRow.headline && (
              <p className="text-sm font-medium text-ink-100">{profileRow.headline}</p>
            )}
            {profileRow.summary && (
              <p className="whitespace-pre-wrap text-sm leading-relaxed text-ink-300">
                {profileRow.summary}
              </p>
            )}
          </div>
        ) : (
          <ProfileAnalysisView analysis={analysis} />
        )}

        {profileRow && (
          <dl className="mt-5 grid grid-cols-2 gap-3 border-t border-ink-800 pt-4 sm:grid-cols-4">
            <Meta label="Profile id" value={String(profileRow.id)} mono />
            <Meta label="Source" value={profileRow.source || '-'} />
            <Meta label="Username" value={profileRow.linkedin_username || '-'} mono />
            <Meta label="Fetched" value={fmtDateTime(profileRow.fetched_at)} />
          </dl>
        )}
      </Card>
    </div>
  );
}

function Meta({ label, value, mono }: { label: string; value: string; mono?: boolean }) {
  return (
    <div className="min-w-0">
      <dt className="text-2xs font-semibold uppercase tracking-wider text-ink-400">{label}</dt>
      <dd
        className={cx('mt-0.5 truncate text-xs text-ink-100', mono && 'font-mono')}
        title={value}
      >
        {value}
      </dd>
    </div>
  );
}
