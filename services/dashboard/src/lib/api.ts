/**
 * Typed fetch wrapper for the whole hermes-core HTTP API, plus an EventSource
 * helper for the two SSE endpoints.
 *
 * Base URL comes from VITE_API_BASE and defaults to `/api`, which nginx proxies
 * to http://hermes-core:8080 (see nginx.conf).
 *
 * Every list endpoint goes through `unwrapList()` and every `*_json` column
 * through `parseJsonish()`, because hermes-core is free to return a bare array
 * or an `{items: [...]}` envelope, and JSON columns as objects or as strings.
 */

import type {
  AtsBreakdown,
  ContainerInfo,
  ContainerStats,
  Health,
  Id,
  Job,
  JobListQuery,
  JobSearchParams,
  JobStatus,
  LinkedInLoginResponse,
  LlmModel,
  LlmTestResponse,
  MatchBreakdown,
  McpHealth,
  Profile,
  ProfileAnalysis,
  ProfileResponse,
  Resume,
  ResumeFormat,
  Run,
  RunEvent,
  SandboxExecResponse,
  SettingsMap,
  StreamLine,
} from './types';

/* -------------------------------------------------------------------------- */
/* Base URL                                                                    */
/* -------------------------------------------------------------------------- */

const RAW_BASE = (import.meta.env.VITE_API_BASE as string | undefined) ?? '/api';

/** Normalized API root, never with a trailing slash. */
export const API_BASE = RAW_BASE.replace(/\/+$/, '');

type QueryValue = string | number | boolean | null | undefined;

/** Build an absolute API URL, dropping empty query params. */
export function apiUrl(path: string, query?: Record<string, QueryValue>): string {
  const suffix = path.startsWith('/') ? path : `/${path}`;
  const base = `${API_BASE}${suffix}`;
  if (!query) return base;

  const qs = new URLSearchParams();
  for (const [key, value] of Object.entries(query)) {
    if (value === undefined || value === null || value === '') continue;
    qs.set(key, String(value));
  }
  const encoded = qs.toString();
  if (!encoded) return base;
  return base.includes('?') ? `${base}&${encoded}` : `${base}?${encoded}`;
}

/* -------------------------------------------------------------------------- */
/* Errors                                                                      */
/* -------------------------------------------------------------------------- */

export class ApiError extends Error {
  readonly status: number;
  readonly body: unknown;
  readonly path: string;

  constructor(message: string, status: number, path: string, body?: unknown) {
    super(message);
    this.name = 'ApiError';
    this.status = status;
    this.path = path;
    this.body = body;
  }
}

/** Human-readable message for any thrown value. Never throws itself. */
export function errorMessage(err: unknown): string {
  if (err instanceof ApiError) {
    // A 0 status means the browser never got a response (core down, DNS, CORS).
    if (err.status === 0) {
      return `${err.message} - is hermes-core running and reachable at ${API_BASE}?`;
    }
    return err.message;
  }
  if (err instanceof Error) return err.message;
  if (typeof err === 'string') return err;
  return 'Unexpected error';
}

/** Pull FastAPI's `detail` (string, or a list of validation objects) out of a body. */
function detailFrom(body: unknown): string | null {
  if (!body || typeof body !== 'object') return null;
  const detail = (body as { detail?: unknown; message?: unknown; error?: unknown }).detail;
  if (typeof detail === 'string' && detail.trim()) return detail;
  if (Array.isArray(detail)) {
    const parts = detail
      .map((entry) => {
        if (typeof entry === 'string') return entry;
        if (entry && typeof entry === 'object') {
          const e = entry as { loc?: unknown; msg?: unknown };
          const loc = Array.isArray(e.loc) ? e.loc.join('.') : '';
          const msg = typeof e.msg === 'string' ? e.msg : '';
          return [loc, msg].filter(Boolean).join(': ');
        }
        return '';
      })
      .filter(Boolean);
    if (parts.length) return parts.join('; ');
  }
  const message = (body as { message?: unknown }).message;
  if (typeof message === 'string' && message.trim()) return message;
  const error = (body as { error?: unknown }).error;
  if (typeof error === 'string' && error.trim()) return error;
  return null;
}

/* -------------------------------------------------------------------------- */
/* Core request helper                                                         */
/* -------------------------------------------------------------------------- */

type Method = 'GET' | 'POST' | 'PUT' | 'PATCH' | 'DELETE';

interface RequestOptions {
  body?: unknown;
  form?: FormData;
  query?: Record<string, QueryValue>;
  signal?: AbortSignal;
  /** Milliseconds before the request is aborted. 0 disables the timeout. */
  timeoutMs?: number;
}

const DEFAULT_TIMEOUT_MS = 120_000; // agent turnarounds can be slow

async function req<T>(method: Method, path: string, options: RequestOptions = {}): Promise<T> {
  const { body, form, query, signal, timeoutMs = DEFAULT_TIMEOUT_MS } = options;
  const url = apiUrl(path, query);

  const headers: Record<string, string> = { Accept: 'application/json' };
  let payload: BodyInit | undefined;

  if (form) {
    payload = form; // let the browser set the multipart boundary
  } else if (body !== undefined) {
    headers['Content-Type'] = 'application/json';
    payload = JSON.stringify(body);
  }

  const controller = new AbortController();
  const timer =
    timeoutMs > 0 ? window.setTimeout(() => controller.abort(new DOMException('Request timed out', 'TimeoutError')), timeoutMs) : null;
  if (signal) {
    if (signal.aborted) controller.abort(signal.reason);
    else signal.addEventListener('abort', () => controller.abort(signal.reason), { once: true });
  }

  let response: Response;
  try {
    response = await fetch(url, {
      method,
      headers,
      body: payload,
      signal: controller.signal,
      credentials: 'same-origin',
    });
  } catch (err) {
    if (timer !== null) window.clearTimeout(timer);
    if (err instanceof DOMException && err.name === 'TimeoutError') {
      throw new ApiError(`${method} ${path} timed out after ${Math.round(timeoutMs / 1000)}s`, 0, path);
    }
    if (err instanceof DOMException && err.name === 'AbortError') throw err; // caller cancelled
    throw new ApiError(`${method} ${path} failed: network unreachable`, 0, path, err);
  }
  if (timer !== null) window.clearTimeout(timer);

  const contentType = response.headers.get('content-type') ?? '';
  const isJson = contentType.includes('json');

  let parsed: unknown = null;
  const text = await response.text();
  if (text) {
    if (isJson) {
      try {
        parsed = JSON.parse(text);
      } catch {
        parsed = text;
      }
    } else {
      parsed = text;
    }
  }

  if (!response.ok) {
    const detail = detailFrom(parsed) ?? (typeof parsed === 'string' && parsed.trim() ? parsed.slice(0, 400) : null);
    throw new ApiError(
      `${method} ${path} -> ${response.status} ${response.statusText}${detail ? `: ${detail}` : ''}`,
      response.status,
      path,
      parsed,
    );
  }

  return (parsed as T) ?? (null as unknown as T);
}

/* -------------------------------------------------------------------------- */
/* Shape normalizers                                                           */
/* -------------------------------------------------------------------------- */

const LIST_KEYS = ['items', 'results', 'data', 'jobs', 'runs', 'resumes', 'containers', 'models', 'events', 'rows'];

/** Accept `T[]`, `{items: T[]}`, `{data: {...}}`, or null and always return an array. */
export function unwrapList<T>(payload: unknown): T[] {
  if (Array.isArray(payload)) return payload as T[];
  if (payload && typeof payload === 'object') {
    const record = payload as Record<string, unknown>;
    for (const key of LIST_KEYS) {
      const candidate = record[key];
      if (Array.isArray(candidate)) return candidate as T[];
    }
  }
  return [];
}

/**
 * Read a `*_json` column that may be an object, a JSON string, or absent.
 * Returns `null` rather than throwing on malformed JSON.
 */
export function parseJsonish<T>(value: unknown): T | null {
  if (value === null || value === undefined) return null;
  if (typeof value === 'string') {
    const trimmed = value.trim();
    if (!trimmed || trimmed === 'null') return null;
    try {
      return JSON.parse(trimmed) as T;
    } catch {
      return null;
    }
  }
  if (typeof value === 'object') return value as T;
  return null;
}

/** Settings arrive as a flat map or as Setting rows; normalize to a map. */
function normalizeSettings(payload: unknown): SettingsMap {
  const out: SettingsMap = {};
  if (!payload) return out;

  if (Array.isArray(payload)) {
    for (const row of payload) {
      if (row && typeof row === 'object') {
        const r = row as { key?: unknown; value?: unknown };
        if (typeof r.key === 'string') out[r.key] = r.value == null ? '' : String(r.value);
      }
    }
    return out;
  }

  if (typeof payload === 'object') {
    const record = payload as Record<string, unknown>;
    // Possibly wrapped: {settings: {...}} / {items: [...]}
    if (Array.isArray(record.items) || Array.isArray(record.settings)) {
      return normalizeSettings(record.items ?? record.settings);
    }
    const inner = record.settings;
    if (inner && typeof inner === 'object' && !Array.isArray(inner)) {
      return normalizeSettings(inner);
    }
    for (const [key, value] of Object.entries(record)) {
      if (value === null || value === undefined) {
        out[key] = '';
      } else if (typeof value === 'object') {
        out[key] = JSON.stringify(value);
      } else {
        out[key] = String(value);
      }
    }
  }
  return out;
}

/** GET /profile may be flat, or `{profile, analysis}`. Normalize to one shape. */
function normalizeProfile(payload: unknown): ProfileResponse {
  if (!payload || typeof payload !== 'object') return { profile: null, analysis: null };
  const record = payload as Record<string, unknown>;

  const nested = record.profile;
  if (nested && typeof nested === 'object') {
    const profile = nested as Profile;
    const analysis =
      parseJsonish<ProfileAnalysis>(record.analysis) ?? parseJsonish<ProfileAnalysis>(profile.analysis_json);
    return { profile, analysis };
  }

  // Flat Profile row (or an empty object when nothing has been imported).
  if (!('id' in record)) return { profile: null, analysis: null };
  const profile = payload as Profile;
  const analysis =
    parseJsonish<ProfileAnalysis>(record.analysis) ?? parseJsonish<ProfileAnalysis>(profile.analysis_json);
  return { profile, analysis };
}

/* -------------------------------------------------------------------------- */
/* Convenience readers for JSON columns                                        */
/* -------------------------------------------------------------------------- */

export function jobBreakdown(job: Job | null | undefined): MatchBreakdown | null {
  if (!job) return null;
  return parseJsonish<MatchBreakdown>(job.match_breakdown_json);
}

export function resumeBreakdown(resume: Resume | null | undefined): AtsBreakdown | null {
  if (!resume) return null;
  return parseJsonish<AtsBreakdown>(resume.ats_breakdown_json);
}

/**
 * Best-effort ATS breakdown for a finished ats_score / resume_build run:
 * pipelines stash their payload in result_json under a few plausible keys.
 */
export function runAtsBreakdown(run: Run | null | undefined): AtsBreakdown | null {
  if (!run) return null;
  const result = parseJsonish<Record<string, unknown>>(run.result_json);
  if (!result) return null;
  for (const key of ['ats', 'ats_breakdown', 'score', 'breakdown', 'result']) {
    const candidate = result[key];
    if (candidate && typeof candidate === 'object' && 'subscores' in (candidate as object)) {
      return candidate as AtsBreakdown;
    }
  }
  if ('subscores' in result) return result as AtsBreakdown;
  return null;
}

/* -------------------------------------------------------------------------- */
/* The API surface                                                             */
/* -------------------------------------------------------------------------- */

export const api = {
  /* ---- health ---------------------------------------------------------- */
  health: (signal?: AbortSignal) => req<Health>('GET', '/health', { signal, timeoutMs: 15_000 }),

  /* ---- settings -------------------------------------------------------- */
  getSettings: async (): Promise<SettingsMap> => normalizeSettings(await req<unknown>('GET', '/settings')),

  /**
   * PUT /settings. The body is a flat `{key: value}` map of the rows to upsert;
   * the response is normalized back into a map.
   */
  putSettings: async (values: SettingsMap): Promise<SettingsMap> =>
    normalizeSettings(await req<unknown>('PUT', '/settings', { body: values })),

  /* ---- llm ------------------------------------------------------------- */
  listModels: async (): Promise<LlmModel[]> => {
    const payload = await req<unknown>('GET', '/llm/models', { timeoutMs: 30_000 });
    // OpenAI shape is {object:"list", data:[...]}, handled by unwrapList.
    const models = unwrapList<LlmModel | string>(payload);
    return models.map((entry) => (typeof entry === 'string' ? { id: entry } : entry));
  },

  testLlm: (prompt: string, model?: string) =>
    req<LlmTestResponse>('POST', '/llm/test', {
      body: model ? { prompt, model } : { prompt },
      timeoutMs: 180_000,
    }),

  /* ---- linkedin -------------------------------------------------------- */
  linkedinStatus: (signal?: AbortSignal) =>
    req<McpHealth>('GET', '/linkedin/status', { signal, timeoutMs: 30_000 }),

  linkedinLogin: () => req<LinkedInLoginResponse>('POST', '/linkedin/login', { body: {} }),

  /* ---- profile --------------------------------------------------------- */
  importProfile: (linkedinUsername?: string) =>
    req<Run>('POST', '/profile/import', {
      body: linkedinUsername ? { linkedin_username: linkedinUsername } : {},
    }),

  getProfile: async (signal?: AbortSignal): Promise<ProfileResponse> =>
    normalizeProfile(await req<unknown>('GET', '/profile', { signal })),

  /* ---- resumes --------------------------------------------------------- */
  uploadResume: (file: File) => {
    const form = new FormData();
    form.append('file', file, file.name);
    return req<Record<string, unknown>>('POST', '/resume/upload', { form, timeoutMs: 120_000 });
  },

  generateResume: (body: { profile_id?: Id; target_job_id?: Id }) =>
    req<Run>('POST', '/resume/generate', { body }),

  listResumes: async (signal?: AbortSignal): Promise<Resume[]> =>
    unwrapList<Resume>(await req<unknown>('GET', '/resumes', { signal })),

  getResume: (id: Id, signal?: AbortSignal) => req<Resume>('GET', `/resumes/${id}`, { signal }),

  /** Direct download link (FileResponse) - safe to use as an <a href>. */
  resumeDownloadUrl: (id: Id, fmt: ResumeFormat) => apiUrl(`/resumes/${id}/download`, { fmt }),

  scoreResume: (body: { resume_id: Id; job_id?: Id }) => req<Run>('POST', '/resume/score', { body }),

  /* ---- jobs ------------------------------------------------------------ */
  searchJobs: (params: JobSearchParams) => req<Run>('POST', '/jobs/search', { body: params }),

  listJobs: async (query?: JobListQuery, signal?: AbortSignal): Promise<Job[]> =>
    unwrapList<Job>(
      await req<unknown>('GET', '/jobs', {
        query: query
          ? { status: query.status || undefined, min_score: query.min_score, q: query.q || undefined }
          : undefined,
        signal,
      }),
    ),

  getJob: (id: Id, signal?: AbortSignal) => req<Job>('GET', `/jobs/${id}`, { signal }),

  patchJob: (id: Id, body: { status?: JobStatus; notes?: string }) =>
    req<Job>('PATCH', `/jobs/${id}`, { body }),

  tailorJob: (id: Id) => req<Run>('POST', `/jobs/${id}/tailor`, { body: {} }),

  /* ---- runs ------------------------------------------------------------ */
  listRuns: async (signal?: AbortSignal): Promise<Run[]> =>
    unwrapList<Run>(await req<unknown>('GET', '/runs', { signal })),

  getRun: (id: Id, signal?: AbortSignal) => req<Run>('GET', `/runs/${id}`, { signal }),

  /** SSE path (relative to API_BASE) for a run's live event log. */
  runEventsPath: (id: Id) => `/runs/${id}/events`,

  /* ---- containers ------------------------------------------------------ */
  listContainers: async (signal?: AbortSignal): Promise<ContainerInfo[]> =>
    unwrapList<ContainerInfo>(await req<unknown>('GET', '/containers', { signal, timeoutMs: 30_000 })),

  containerAction: (id: string, action: 'start' | 'stop' | 'restart') =>
    req<Record<string, unknown>>('POST', `/containers/${id}/${action}`, { body: {}, timeoutMs: 90_000 }),

  removeContainer: (id: string, force = false) =>
    req<Record<string, unknown>>('DELETE', `/containers/${id}`, { query: { force }, timeoutMs: 90_000 }),

  containerStats: (id: string, signal?: AbortSignal) =>
    req<ContainerStats>('GET', `/containers/${id}/stats`, { signal, timeoutMs: 20_000 }),

  /** SSE path (relative to API_BASE) for a container's live logs. */
  containerLogsPath: (id: string, tail = 200) => `/containers/${id}/logs?tail=${tail}`,

  /* ---- sandbox --------------------------------------------------------- */
  sandboxExec: (code: string, files?: Record<string, string>) =>
    req<SandboxExecResponse>('POST', '/sandbox/exec', {
      body: files ? { code, files } : { code },
      timeoutMs: 600_000,
    }),
};

/* -------------------------------------------------------------------------- */
/* SSE                                                                         */
/* -------------------------------------------------------------------------- */

export interface SseOptions {
  onOpen?: () => void;
  /** Fired on transport error. EventSource auto-reconnects unless we close. */
  onError?: (event: Event) => void;
}

/**
 * Subscribe to an SSE endpoint under API_BASE.
 *
 * `onEvent` receives the raw `data:` payload of each frame. Returns a disposer
 * that closes the connection - always call it from a useEffect cleanup, or the
 * browser will keep reconnecting forever.
 */
export function sse(path: string, onEvent: (data: string) => void, options: SseOptions = {}): () => void {
  const source = new EventSource(apiUrl(path));

  source.onmessage = (event: MessageEvent<string>) => {
    onEvent(event.data);
  };
  if (options.onOpen) {
    source.onopen = () => options.onOpen?.();
  }
  source.onerror = (event: Event) => {
    options.onError?.(event);
  };

  return () => {
    source.onmessage = null;
    source.onerror = null;
    source.onopen = null;
    source.close();
  };
}

let streamSeq = 0;

/**
 * Decode one SSE `data:` payload into a display line.
 *
 * events.py publishes JSON ({ts, level, message}) for run events, while the
 * container log stream is plain text - both are handled here.
 */
export function decodeStreamLine(raw: string): StreamLine {
  streamSeq += 1;
  const key = `l${streamSeq}`;
  const fallbackTs = new Date().toISOString();

  const trimmed = raw.trim();
  if (trimmed.startsWith('{')) {
    const parsed = parseJsonish<RunEvent & { msg?: string; text?: string; event?: string }>(trimmed);
    if (parsed) {
      const message =
        parsed.message ?? parsed.msg ?? parsed.text ?? parsed.event ?? JSON.stringify(parsed);
      return {
        key,
        ts: parsed.ts ?? fallbackTs,
        level: (parsed.level ?? 'info').toString().toLowerCase(),
        message: String(message),
      };
    }
  }

  // Plain text: infer a level so errors still stand out in the log viewer.
  const lowered = trimmed.toLowerCase();
  let level: string = 'info';
  if (/\b(error|traceback|exception|fatal|critical)\b/.test(lowered)) level = 'error';
  else if (/\b(warn|warning|deprecat)\b/.test(lowered)) level = 'warning';
  else if (/\bdebug\b/.test(lowered)) level = 'debug';

  return { key, ts: fallbackTs, level, message: raw };
}
