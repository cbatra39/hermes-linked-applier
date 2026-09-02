/**
 * Settings — the one place where Hermes is configured, and the one place that
 * tells you honestly what is still missing.
 *
 * Four blocks:
 *   1. Missing configuration — computed from /api/health + /api/settings, each
 *      item with the concrete fix.
 *   2. Editable settings (PUT /api/settings). Only *changed* keys are sent, so a
 *      masked secret is never written back over the real one.
 *   3. Model picker fed by /api/llm/models, with a live "Test model" round trip
 *      against /api/llm/test that reports latency and the actual output.
 *   4. Read-only sandbox limits, so you can see what the sandbox will enforce
 *      without being able to weaken it from the browser.
 */

import { useCallback, useMemo, useState } from 'react';
import { Link } from 'react-router-dom';

import { Badge, Button, Card, ErrorState, Spinner, StatusDot } from '../components';
import { api } from '../lib/api';
import { cx, fmtNum, truncate } from '../lib/format';
import { useAction, useApi } from '../lib/hooks';
import { SANDBOX_LIMIT_KEYS, type Health, type LlmTestResponse, type SettingsMap } from '../lib/types';

/* -------------------------------------------------------------------------- */
/* Field definitions                                                           */
/* -------------------------------------------------------------------------- */

type FieldKind = 'text' | 'url' | 'secret' | 'model' | 'model-csv';

interface FieldDef {
  key: string;
  label: string;
  help: string;
  kind: FieldKind;
  placeholder?: string;
}

/**
 * The keys Hermes actually reads (settings.py field names). Anything else the API
 * returns is still rendered, generically, at the bottom of the form — hiding an
 * unknown row would make it unsavable.
 */
const FIELDS: readonly FieldDef[] = [
  {
    key: 'freellmapi_key',
    label: 'freellmapi API key',
    kind: 'secret',
    placeholder: 'freellmapi-...',
    help: 'Client token minted in the freellmapi dashboard. Every agent call fails without it.',
  },
  {
    key: 'freellmapi_base_url',
    label: 'freellmapi base URL',
    kind: 'url',
    placeholder: 'http://freellmapi:3001/v1',
    help: 'OpenAI-compatible endpoint. Inside compose this is the service name, not localhost.',
  },
  {
    key: 'hermes_model_primary',
    label: 'Primary model',
    kind: 'model',
    placeholder: '(auto-pick the first model the router offers)',
    help: 'Leave blank to let Hermes pick the first model from GET /v1/models.',
  },
  {
    key: 'hermes_model_fallbacks',
    label: 'Fallback models',
    kind: 'model-csv',
    placeholder: 'model-a, model-b',
    help: 'Tried in order when the primary model errors or rate-limits. Comma-separated.',
  },
  {
    key: 'linkedin_mcp_url',
    label: 'LinkedIn MCP URL',
    kind: 'url',
    placeholder: 'http://linkedin-mcp:8000/mcp',
    help: 'Streamable-HTTP endpoint of the linkedin-mcp container.',
  },
] as const;

const FIELD_KEYS = new Set(FIELDS.map((field) => field.key));

/** Never render these as editable text, and never echo them into the DOM. */
const HIDDEN_KEYS = new Set(['encryption_key']);

const SANDBOX_LABELS: Record<string, string> = {
  hermes_sandbox_image: 'Image',
  hermes_sandbox_memory_mb: 'Memory limit (MB)',
  hermes_sandbox_cpus: 'CPU limit',
  hermes_sandbox_timeout_s: 'Wall-clock timeout (s)',
  hermes_sandbox_network: 'Networking',
  hermes_sandbox_workspace: 'Workspace root',
};

const DEFAULT_TEST_PROMPT = 'Reply with exactly: hermes ok';

/* -------------------------------------------------------------------------- */
/* Helpers                                                                     */
/* -------------------------------------------------------------------------- */

function pick<T>(source: unknown, key: string): T | undefined {
  if (source && typeof source === 'object' && key in source) {
    return (source as Record<string, unknown>)[key] as T;
  }
  return undefined;
}

/** Does this look like a value the server already redacted for us? */
function looksMasked(value: string): boolean {
  return /[•*]{3,}|…/.test(value);
}

/**
 * `freellmapi-abcd••••••••wxyz` — keeps the recognisable prefix and the last few
 * characters so you can tell two keys apart without exposing the middle.
 */
function maskSecret(value: string): string {
  const trimmed = value.trim();
  if (!trimmed) return '';
  if (looksMasked(trimmed)) return trimmed;
  if (trimmed.length <= 10) return '•'.repeat(trimmed.length);
  const head = trimmed.slice(0, Math.min(15, trimmed.length - 8));
  const tail = trimmed.slice(-4);
  return `${head}${'•'.repeat(8)}${tail}`;
}

function csvToList(value: string): string[] {
  return value
    .replace(/;/g, ',')
    .split(',')
    .map((part) => part.trim())
    .filter(Boolean);
}

interface Issue {
  key: string;
  severity: 'error' | 'warn';
  title: string;
  detail: string;
  to?: string;
  cta?: string;
  href?: string;
}

function buildIssues(
  values: SettingsMap,
  health: Health | null,
  modelCount: number | null,
  routerPort: string,
): Issue[] {
  const issues: Issue[] = [];
  const llm = health?.llm;
  const mcp = health?.mcp;

  const keyValue = (values.freellmapi_key ?? '').trim();
  const keyConfigured = Boolean(llm?.key_configured) || keyValue.length > 0;

  if (!keyConfigured) {
    issues.push({
      key: 'llm-key',
      severity: 'error',
      title: 'FREELLMAPI_KEY is not set',
      detail:
        `Open the freellmapi dashboard on port ${routerPort}, mint a client token (it starts with ` +
        '"freellmapi-"), and save it in the freellmapi API key field below. Until then every agent — ' +
        'profile analysis, resume writing, match ranking, ATS scoring — will fail.',
      href: `http://127.0.0.1:${routerPort}`,
      cta: 'Open freellmapi',
    });
  } else if (keyValue && !looksMasked(keyValue) && !keyValue.startsWith('freellmapi-')) {
    issues.push({
      key: 'llm-key-prefix',
      severity: 'warn',
      title: 'The API key does not look like a freellmapi token',
      detail:
        'freellmapi client tokens start with "freellmapi-". The router will probably answer 401. ' +
        'Re-copy the token from its dashboard.',
    });
  }

  if (!(values.freellmapi_base_url ?? llm?.base_url ?? '').trim()) {
    issues.push({
      key: 'llm-base',
      severity: 'error',
      title: 'FREELLMAPI_BASE_URL is empty',
      detail: 'Expected http://freellmapi:3001/v1 — the service name inside the compose network.',
    });
  }

  const llmReachable = Boolean(pick<boolean>(llm, 'reachable') ?? llm?.ok);
  if (keyConfigured && !llmReachable) {
    issues.push({
      key: 'llm-unreachable',
      severity: 'error',
      title: 'The LLM router is not answering',
      detail:
        (llm?.detail || llm?.error || 'No detail returned by /api/health.') +
        ' Check `make logs freellmapi` — the container needs ENCRYPTION_KEY set in the root .env to boot.',
    });
  } else if (llmReachable && modelCount === 0) {
    issues.push({
      key: 'llm-no-models',
      severity: 'warn',
      title: 'The router returned zero models',
      detail:
        'GET /v1/models came back empty, so there is nothing to auto-pick. Add at least one provider ' +
        `in the freellmapi dashboard on port ${routerPort}.`,
      href: `http://127.0.0.1:${routerPort}`,
      cta: 'Open freellmapi',
    });
  }

  if (!(values.linkedin_mcp_url ?? '').trim() && !pick<string>(mcp, 'url')) {
    issues.push({
      key: 'mcp-url',
      severity: 'error',
      title: 'LINKEDIN_MCP_URL is empty',
      detail: 'Expected http://linkedin-mcp:8000/mcp. Job search and profile import both need it.',
    });
  } else if (!mcp?.reachable) {
    issues.push({
      key: 'mcp-down',
      severity: 'error',
      title: 'linkedin-mcp is unreachable',
      detail: mcp?.detail || 'Nothing answered at the MCP endpoint. Start the stack with `make up`.',
      to: '/linkedin',
      cta: 'LinkedIn',
    });
  } else if (!mcp?.authenticated) {
    issues.push({
      key: 'mcp-session',
      severity: 'warn',
      title: 'LinkedIn is not connected',
      detail:
        'linkedin-mcp is running but has no session. Sign in once by hand — Hermes cannot do it for ' +
        'you, and there is no way around the 2FA/captcha step.',
      to: '/linkedin',
      cta: 'Connect',
    });
  }

  if (health && !health.docker) {
    issues.push({
      key: 'docker',
      severity: 'warn',
      title: 'Docker is not reachable from hermes-core',
      detail:
        'The container panel and the code sandbox are both disabled. Mount ' +
        '/var/run/docker.sock into hermes-core and run `make restart`.',
      to: '/containers',
      cta: 'Containers',
    });
  }

  return issues;
}

/* -------------------------------------------------------------------------- */
/* Small presentational pieces                                                 */
/* -------------------------------------------------------------------------- */

function IssueRow({ issue }: { issue: Issue }) {
  const isError = issue.severity === 'error';
  return (
    <li
      className={cx(
        'flex flex-wrap items-start gap-3 rounded-md border p-3',
        isError ? 'border-bad-600/40 bg-bad-500/5' : 'border-warn-600/40 bg-warn-500/5',
      )}
    >
      <StatusDot tone={isError ? 'bad' : 'warn'} />
      <div className="min-w-0 flex-1">
        <p className={cx('text-sm font-medium', isError ? 'text-bad-400' : 'text-warn-400')}>
          {issue.title}
        </p>
        <p className="mt-1 text-xs leading-relaxed text-ink-200">{issue.detail}</p>
      </div>
      {issue.to && issue.cta && (
        <Link
          to={issue.to}
          className="shrink-0 rounded-md border border-ink-700 bg-ink-800 px-2.5 py-1 text-xs font-medium text-ink-100 hover:border-brand-600 hover:text-brand-200"
        >
          {issue.cta}
        </Link>
      )}
      {issue.href && issue.cta && (
        <a
          href={issue.href}
          target="_blank"
          rel="noopener noreferrer"
          className="shrink-0 rounded-md border border-ink-700 bg-ink-800 px-2.5 py-1 text-xs font-medium text-ink-100 hover:border-brand-600 hover:text-brand-200"
        >
          {issue.cta}
        </a>
      )}
    </li>
  );
}

const INPUT_CLS =
  'block w-full rounded-md border border-ink-700 bg-ink-950 px-3 py-2 text-sm text-ink-100 ' +
  'placeholder:text-ink-500 focus:border-brand-600 focus:outline-none focus:ring-1 focus:ring-brand-600';

/* -------------------------------------------------------------------------- */
/* Page                                                                        */
/* -------------------------------------------------------------------------- */

export default function Settings() {
  const [drafts, setDrafts] = useState<Record<string, string>>({});
  const [revealed, setRevealed] = useState<Record<string, boolean>>({});
  const [savedNote, setSavedNote] = useState<string | null>(null);
  const [testPrompt, setTestPrompt] = useState<string>(DEFAULT_TEST_PROMPT);
  const [testModel, setTestModel] = useState<string>('');
  const [testResult, setTestResult] = useState<LlmTestResponse | null>(null);

  const settings = useApi<SettingsMap>(() => api.getSettings(), []);
  const models = useApi(() => api.listModels(), []);
  const health = useApi<Health>((signal) => api.health(signal), [], { intervalMs: 30_000 });

  const serverValues = settings.data ?? {};
  const modelList = models.data ?? [];

  const valueOf = useCallback(
    (key: string): string => drafts[key] ?? serverValues[key] ?? '',
    [drafts, serverValues],
  );

  const dirtyKeys = useMemo(
    () => Object.keys(drafts).filter((key) => drafts[key] !== (serverValues[key] ?? '')),
    [drafts, serverValues],
  );

  const setDraft = (key: string, value: string) => {
    setSavedNote(null);
    setDrafts((previous) => ({ ...previous, [key]: value }));
  };

  const routerPort = (serverValues.freellmapi_port ?? '3001').trim() || '3001';

  const modelCount = models.error ? null : modelList.length;

  const issues = useMemo(
    () => buildIssues({ ...serverValues, ...drafts }, health.data, modelCount, routerPort),
    [serverValues, drafts, health.data, modelCount, routerPort],
  );

  /* ---- save ------------------------------------------------------------- */
  const save = useAction(async () => {
    const payload: SettingsMap = {};
    for (const key of dirtyKeys) payload[key] = drafts[key];
    const result = await api.putSettings(payload);
    if (Object.keys(result).length > 0) {
      settings.setData(result);
    } else {
      settings.reload();
    }
    setDrafts({});
    setRevealed({});
    setSavedNote(
      `Saved ${dirtyKeys.length} setting${dirtyKeys.length === 1 ? '' : 's'}. ` +
        'Re-checking health…',
    );
    health.reload();
    models.reload();
    return result;
  });

  /* ---- test the model --------------------------------------------------- */
  const test = useAction(async () => {
    setTestResult(null);
    const chosen = testModel.trim() || valueOf('hermes_model_primary').trim();
    const response = await api.testLlm(testPrompt.trim() || DEFAULT_TEST_PROMPT, chosen || undefined);
    setTestResult(response);
    return response;
  });

  /* ---- unknown keys the API returned that we do not model --------------- */
  const extraKeys = useMemo(
    () =>
      Object.keys(serverValues)
        .filter((key) => !FIELD_KEYS.has(key) && !HIDDEN_KEYS.has(key))
        .filter((key) => !SANDBOX_LIMIT_KEYS.includes(key))
        .sort(),
    [serverValues],
  );

  /* ---- sandbox limits (read-only) --------------------------------------- */
  const sandboxRows = useMemo(() => {
    const fromHealth =
      pick<Record<string, unknown>>(health.data, 'sandbox') ??
      pick<Record<string, unknown>>(pick<unknown>(health.data, 'config'), 'sandbox') ??
      null;

    return SANDBOX_LIMIT_KEYS.map((key) => {
      const short = key.replace(/^hermes_sandbox_/, '');
      const raw =
        serverValues[key] ??
        (fromHealth ? pick<unknown>(fromHealth, short) : undefined) ??
        undefined;
      const text =
        raw === undefined || raw === null || raw === ''
          ? null
          : typeof raw === 'object'
            ? JSON.stringify(raw)
            : String(raw);
      return { key, label: SANDBOX_LABELS[key] ?? short, value: text };
    });
  }, [serverValues, health.data]);

  const sandboxKnown = sandboxRows.some((row) => row.value !== null);

  const llmTone = health.data?.llm?.key_configured
    ? pick<boolean>(health.data?.llm, 'reachable') ?? health.data?.llm?.ok
      ? 'good'
      : 'bad'
    : 'warn';

  const secretIsSet = Boolean(
    (serverValues.freellmapi_key ?? '').trim() || health.data?.llm?.key_configured,
  );

  return (
    <div className="space-y-6">
      {/* ------------------------------------------------------------ header */}
      <header className="flex flex-wrap items-end justify-between gap-3">
        <div>
          <h1 className="text-xl font-semibold tracking-tight text-ink-100">Settings</h1>
          <p className="mt-1 text-sm text-ink-400">
            Credentials, model routing and the sandbox contract. Changes are stored in Hermes' own
            settings table.
          </p>
        </div>
        <div className="flex items-center gap-2">
          {(settings.refreshing || health.refreshing) && <Spinner size="sm" label="Refreshing" />}
          <Button
            variant="ghost"
            onClick={() => {
              setDrafts({});
              setRevealed({});
              setSavedNote(null);
              settings.reload();
              models.reload();
              health.reload();
            }}
            disabled={save.busy}
          >
            Discard & reload
          </Button>
          <Button onClick={() => void save.run()} busy={save.busy} disabled={save.busy || dirtyKeys.length === 0}>
            {dirtyKeys.length === 0 ? 'No changes' : `Save ${dirtyKeys.length} change${dirtyKeys.length === 1 ? '' : 's'}`}
          </Button>
        </div>
      </header>

      {save.error && <ErrorState title="Could not save settings" message={save.error} onRetry={() => void save.run()} />}

      {savedNote && !save.error && (
        <div
          role="status"
          className="flex items-center justify-between gap-3 rounded-lg border border-good-600/40 bg-good-500/10 px-3 py-2 text-xs text-good-400"
        >
          <span>{savedNote}</span>
          <button
            type="button"
            onClick={() => setSavedNote(null)}
            className="text-ink-300 hover:text-ink-100"
            aria-label="Dismiss"
          >
            ✕
          </button>
        </div>
      )}

      {/* -------------------------------------------------- missing config */}
      <Card
        title="Configuration status"
        subtitle={
          issues.length === 0
            ? 'Everything Hermes needs is present'
            : `${issues.length} item${issues.length === 1 ? '' : 's'} need attention`
        }
        actions={
          issues.length === 0 ? (
            <Badge tone="good">complete</Badge>
          ) : (
            <Badge tone={issues.some((issue) => issue.severity === 'error') ? 'bad' : 'warn'}>
              {issues.filter((issue) => issue.severity === 'error').length} blocking
            </Badge>
          )
        }
      >
        {health.error ? (
          <ErrorState
            title="Cannot read health"
            message={health.error}
            onRetry={() => health.reload()}
          />
        ) : health.loading ? (
          <Spinner label="Probing dependencies" />
        ) : issues.length === 0 ? (
          <p className="text-sm text-ink-300">
            The LLM router answers, linkedin-mcp holds a session, and Docker is reachable. Nothing is
            missing.
          </p>
        ) : (
          <ul className="space-y-2">
            {issues.map((issue) => (
              <IssueRow key={issue.key} issue={issue} />
            ))}
          </ul>
        )}
      </Card>

      {/* ------------------------------------------------------------- form */}
      <Card
        title="Credentials & routing"
        subtitle="Only the fields you change are sent to the API"
        actions={
          <div className="flex items-center gap-2">
            <StatusDot tone={llmTone} />
            <span className="text-2xs text-ink-400">
              {health.data?.llm?.primary ? `primary: ${health.data.llm.primary}` : 'no primary model'}
            </span>
          </div>
        }
      >
        {settings.error ? (
          <ErrorState
            title="Could not load settings"
            message={settings.error}
            onRetry={() => settings.reload()}
          />
        ) : settings.loading ? (
          <Spinner label="Loading settings" />
        ) : (
          <div className="space-y-5">
            {FIELDS.map((field) => {
              const value = valueOf(field.key);
              const dirty = dirtyKeys.includes(field.key);
              const inputId = `setting-${field.key}`;

              return (
                <div key={field.key}>
                  <div className="mb-1.5 flex items-baseline justify-between gap-2">
                    <label htmlFor={inputId} className="text-xs font-medium text-ink-200">
                      {field.label}
                      <span className="ml-2 font-mono text-2xs text-ink-400">{field.key}</span>
                    </label>
                    {dirty && <Badge tone="warn">unsaved</Badge>}
                  </div>

                  {field.kind === 'secret' ? (
                    <SecretField
                      id={inputId}
                      value={value}
                      revealed={Boolean(revealed[field.key])}
                      isSet={secretIsSet}
                      placeholder={field.placeholder}
                      onToggleReveal={() =>
                        setRevealed((previous) => ({ ...previous, [field.key]: !previous[field.key] }))
                      }
                      onChange={(next) => setDraft(field.key, next)}
                    />
                  ) : field.kind === 'model' ? (
                    <ModelField
                      id={inputId}
                      value={value}
                      options={modelList.map((model) => model.id)}
                      placeholder={field.placeholder}
                      onChange={(next) => setDraft(field.key, next)}
                    />
                  ) : field.kind === 'model-csv' ? (
                    <ModelCsvField
                      id={inputId}
                      value={value}
                      options={modelList.map((model) => model.id)}
                      placeholder={field.placeholder}
                      onChange={(next) => setDraft(field.key, next)}
                    />
                  ) : (
                    <input
                      id={inputId}
                      type={field.kind === 'url' ? 'url' : 'text'}
                      value={value}
                      placeholder={field.placeholder}
                      autoComplete="off"
                      spellCheck={false}
                      onChange={(event) => setDraft(field.key, event.target.value)}
                      className={INPUT_CLS}
                    />
                  )}

                  <p className="mt-1 text-2xs leading-relaxed text-ink-400">{field.help}</p>
                  {field.key === 'freellmapi_key' && (
                    <p className="mt-1 text-2xs leading-relaxed text-ink-400">
                      Get one from the freellmapi dashboard at{' '}
                      <a
                        href={`http://127.0.0.1:${routerPort}`}
                        target="_blank"
                        rel="noopener noreferrer"
                        className="text-brand-300 underline decoration-dotted"
                      >
                        http://127.0.0.1:{routerPort}
                      </a>{' '}
                      → sign in → mint a client token. The router container itself also needs{' '}
                      <code className="font-mono text-ink-300">ENCRYPTION_KEY</code> in the root{' '}
                      <code className="font-mono text-ink-300">.env</code>; that one is never editable
                      from the browser.
                    </p>
                  )}
                </div>
              );
            })}

            {models.error && (
              <p className="text-2xs leading-relaxed text-warn-400">
                Could not list models ({truncate(models.error, 160)}). Type a model id by hand, or fix
                the router and reload.
              </p>
            )}

            {extraKeys.length > 0 && (
              <div className="border-t border-ink-800 pt-4">
                <p className="mb-3 text-2xs font-semibold uppercase tracking-wider text-ink-400">
                  Other stored settings
                </p>
                <div className="space-y-3">
                  {extraKeys.map((key) => {
                    const inputId = `setting-extra-${key}`;
                    const dirty = dirtyKeys.includes(key);
                    return (
                      <div key={key}>
                        <div className="mb-1 flex items-baseline justify-between gap-2">
                          <label htmlFor={inputId} className="font-mono text-2xs text-ink-300">
                            {key}
                          </label>
                          {dirty && <Badge tone="warn">unsaved</Badge>}
                        </div>
                        <input
                          id={inputId}
                          type="text"
                          value={valueOf(key)}
                          autoComplete="off"
                          spellCheck={false}
                          onChange={(event) => setDraft(key, event.target.value)}
                          className={INPUT_CLS}
                        />
                      </div>
                    );
                  })}
                </div>
              </div>
            )}

            <p className="border-t border-ink-800 pt-4 text-2xs leading-relaxed text-ink-400">
              These rows live in Hermes' settings table. If a value you save here does not take effect,
              the process environment is overriding it — edit the root{' '}
              <code className="font-mono text-ink-300">.env</code> and run{' '}
              <code className="font-mono text-brand-300">make restart</code>. The read-only block below
              shows what the running process actually loaded.
            </p>
          </div>
        )}
      </Card>

      {/* -------------------------------------------------------- test model */}
      <Card
        title="Test model"
        subtitle="One real round trip through the router — latency and raw output"
      >
        <div className="grid grid-cols-1 gap-4 lg:grid-cols-3">
          <div className="lg:col-span-2">
            <label htmlFor="test-prompt" className="mb-1.5 block text-xs font-medium text-ink-200">
              Prompt
            </label>
            <textarea
              id="test-prompt"
              rows={3}
              value={testPrompt}
              onChange={(event) => setTestPrompt(event.target.value)}
              className={cx(INPUT_CLS, 'resize-y font-mono text-xs')}
              placeholder={DEFAULT_TEST_PROMPT}
            />
          </div>
          <div>
            <label htmlFor="test-model" className="mb-1.5 block text-xs font-medium text-ink-200">
              Model
            </label>
            <select
              id="test-model"
              value={testModel}
              onChange={(event) => setTestModel(event.target.value)}
              className={INPUT_CLS}
            >
              <option value="">
                {valueOf('hermes_model_primary').trim()
                  ? `use primary (${valueOf('hermes_model_primary').trim()})`
                  : 'let Hermes choose'}
              </option>
              {modelList.map((model) => (
                <option key={model.id} value={model.id}>
                  {model.id}
                  {model.owned_by ? ` — ${model.owned_by}` : ''}
                </option>
              ))}
            </select>
            <p className="mt-1 text-2xs text-ink-400">
              {models.loading
                ? 'Loading models…'
                : `${modelList.length} model${modelList.length === 1 ? '' : 's'} offered by the router`}
            </p>
          </div>
        </div>

        <div className="mt-3 flex flex-wrap items-center gap-2">
          <Button onClick={() => void test.run()} busy={test.busy} disabled={test.busy || !testPrompt.trim()}>
            {test.busy ? 'Calling the router…' : 'Test model'}
          </Button>
          <Button
            variant="ghost"
            onClick={() => {
              setTestResult(null);
              test.clearError();
              setTestPrompt(DEFAULT_TEST_PROMPT);
            }}
            disabled={test.busy}
          >
            Reset
          </Button>
          {dirtyKeys.length > 0 && (
            <span className="text-2xs text-warn-400">
              Unsaved changes are not used by this test — save first.
            </span>
          )}
        </div>

        {test.error && (
          <div className="mt-3">
            <ErrorState title="Model test failed" message={test.error} onRetry={() => void test.run()} />
          </div>
        )}

        {testResult && (
          <div className="mt-4 space-y-2 border-t border-ink-800 pt-4">
            <div className="flex flex-wrap items-center gap-2">
              <Badge tone="good">ok</Badge>
              {testResult.model && (
                <span className="font-mono text-2xs text-ink-200">{testResult.model}</span>
              )}
              {typeof testResult.latency_ms === 'number' && (
                <Badge tone={testResult.latency_ms > 8000 ? 'warn' : 'neutral'}>
                  {fmtNum(testResult.latency_ms, 0)} ms
                </Badge>
              )}
            </div>
            <div>
              <p className="mb-1 text-2xs font-semibold uppercase tracking-wider text-ink-400">output</p>
              <pre className="max-h-64 overflow-auto whitespace-pre-wrap break-words rounded-md border border-ink-700 bg-ink-950 p-3 font-mono text-2xs leading-relaxed text-ink-100">
                {testResult.output?.trim() || '(the model returned an empty string)'}
              </pre>
            </div>
          </div>
        )}
      </Card>

      {/* ---------------------------------------------------- sandbox limits */}
      <Card
        title="Sandbox limits"
        subtitle="Read-only — security invariants are not editable from the browser"
        actions={<Badge tone="neutral">enforced by hermes-core</Badge>}
      >
        {sandboxKnown ? (
          <dl className="grid grid-cols-1 gap-3 sm:grid-cols-2 lg:grid-cols-3">
            {sandboxRows.map((row) => (
              <div key={row.key} className="rounded-md border border-ink-800 bg-ink-850 p-3">
                <dt className="text-2xs font-semibold uppercase tracking-wider text-ink-400">
                  {row.label}
                </dt>
                <dd className="mt-1 break-words font-mono text-xs text-ink-100">
                  {row.value ?? <span className="text-ink-400">not reported</span>}
                </dd>
              </div>
            ))}
          </dl>
        ) : (
          <p className="text-xs leading-relaxed text-ink-300">
            hermes-core did not report its sandbox limits. They come from the root{' '}
            <code className="font-mono text-ink-200">.env</code> (
            <code className="font-mono text-ink-200">HERMES_SANDBOX_*</code>) and are applied at
            startup.
          </p>
        )}

        <ul className="mt-4 grid grid-cols-1 gap-1 border-t border-ink-800 pt-4 text-2xs leading-relaxed text-ink-400 sm:grid-cols-2">
          <li>Root filesystem mounted read-only; only a 64 MB tmpfs on /tmp is writable</li>
          <li>Runs as uid 1000 with every Linux capability dropped and no-new-privileges</li>
          <li>PID limit 256; the container is killed when the wall-clock timeout expires</li>
          <li>Networking is off by default — sandboxed code cannot reach the internet or your host</li>
        </ul>
        <p className="mt-3 text-2xs text-ink-400">
          Try it from the{' '}
          <Link to="/containers" className="text-brand-300 underline decoration-dotted">
            Containers page
          </Link>
          .
        </p>
      </Card>
    </div>
  );
}

/* -------------------------------------------------------------------------- */
/* Field components                                                            */
/* -------------------------------------------------------------------------- */

function SecretField({
  id,
  value,
  revealed,
  isSet,
  placeholder,
  onToggleReveal,
  onChange,
}: {
  id: string;
  value: string;
  revealed: boolean;
  isSet: boolean;
  placeholder?: string;
  onToggleReveal: () => void;
  onChange: (next: string) => void;
}) {
  const masked = maskSecret(value);

  return (
    <div className="space-y-2">
      {!revealed && value ? (
        // Masked display: shows that a key exists, and which one, without echoing it.
        <div className="flex items-center gap-2">
          <output
            htmlFor={id}
            className="block flex-1 truncate rounded-md border border-ink-700 bg-ink-900 px-3 py-2 font-mono text-sm text-ink-200"
          >
            {masked}
          </output>
          <Button size="sm" variant="ghost" onClick={onToggleReveal}>
            Reveal
          </Button>
        </div>
      ) : (
        <div className="flex items-center gap-2">
          <input
            id={id}
            type={revealed ? 'text' : 'password'}
            value={value}
            placeholder={placeholder}
            autoComplete="off"
            spellCheck={false}
            onChange={(event) => onChange(event.target.value)}
            className={cx(INPUT_CLS, 'flex-1 font-mono')}
          />
          {value && (
            <Button size="sm" variant="ghost" onClick={onToggleReveal}>
              {revealed ? 'Hide' : 'Show'}
            </Button>
          )}
        </div>
      )}

      {!revealed && value && (
        <div className="flex flex-wrap items-center gap-2">
          <Button size="sm" variant="secondary" onClick={() => onChange('')}>
            Replace key
          </Button>
          <span className="text-2xs text-ink-400">
            Clears the field so you can paste a new token. Nothing is sent until you save.
          </span>
        </div>
      )}

      {!value && (
        <p className="text-2xs text-ink-400">
          {isSet
            ? 'The stored key is not exposed by the API. Paste a new token to replace it, or leave blank to keep it.'
            : 'No key stored yet.'}
        </p>
      )}
    </div>
  );
}

function ModelField({
  id,
  value,
  options,
  placeholder,
  onChange,
}: {
  id: string;
  value: string;
  options: string[];
  placeholder?: string;
  onChange: (next: string) => void;
}) {
  const known = options.includes(value);

  return (
    <div className="space-y-2">
      <select
        id={id}
        value={known ? value : value ? '__custom__' : ''}
        onChange={(event) => {
          const next = event.target.value;
          if (next === '__custom__') return; // keep whatever is typed below
          onChange(next);
        }}
        className={INPUT_CLS}
      >
        <option value="">{placeholder ?? '(auto)'}</option>
        {options.map((option) => (
          <option key={option} value={option}>
            {option}
          </option>
        ))}
        {value && !known && <option value="__custom__">{value} (not offered by the router)</option>}
      </select>
      <input
        type="text"
        value={value}
        placeholder="…or type a model id by hand"
        autoComplete="off"
        spellCheck={false}
        onChange={(event) => onChange(event.target.value)}
        className={cx(INPUT_CLS, 'font-mono text-xs')}
        aria-label="Model id"
      />
    </div>
  );
}

function ModelCsvField({
  id,
  value,
  options,
  placeholder,
  onChange,
}: {
  id: string;
  value: string;
  options: string[];
  placeholder?: string;
  onChange: (next: string) => void;
}) {
  const list = csvToList(value);
  const available = options.filter((option) => !list.includes(option));

  const remove = (model: string) => onChange(list.filter((entry) => entry !== model).join(', '));
  const add = (model: string) => {
    if (!model || list.includes(model)) return;
    onChange([...list, model].join(', '));
  };

  return (
    <div className="space-y-2">
      <input
        id={id}
        type="text"
        value={value}
        placeholder={placeholder}
        autoComplete="off"
        spellCheck={false}
        onChange={(event) => onChange(event.target.value)}
        className={cx(INPUT_CLS, 'font-mono text-xs')}
      />
      {list.length > 0 && (
        <ul className="flex flex-wrap gap-1.5">
          {list.map((model, index) => (
            <li
              key={model}
              className="inline-flex items-center gap-1.5 rounded-md border border-ink-700 bg-ink-800 px-2 py-0.5 font-mono text-2xs text-ink-100"
            >
              <span className="tabular-nums text-ink-400">{index + 1}.</span>
              {model}
              <button
                type="button"
                onClick={() => remove(model)}
                className="text-ink-400 hover:text-bad-400"
                aria-label={`Remove ${model}`}
              >
                ✕
              </button>
            </li>
          ))}
        </ul>
      )}
      {available.length > 0 && (
        <select
          value=""
          onChange={(event) => add(event.target.value)}
          className={cx(INPUT_CLS, 'text-xs')}
          aria-label="Add a fallback model"
        >
          <option value="">+ add a model the router offers…</option>
          {available.map((option) => (
            <option key={option} value={option}>
              {option}
            </option>
          ))}
        </select>
      )}
    </div>
  );
}
