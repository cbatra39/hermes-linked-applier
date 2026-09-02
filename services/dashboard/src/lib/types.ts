/**
 * TypeScript mirrors of the hermes-core DB models (services/core/hermes/models.py)
 * and of the /api response payloads.
 *
 * Defensive by design: hermes-core stores every `*_json` column as TEXT holding a
 * JSON string, and may hand it back either already-parsed or still as a string.
 * Anything of type `Jsonish<T>` must therefore be read through `parseJsonish()`
 * from ./api rather than used directly.
 */

/** A value that may arrive parsed, as a JSON string, or missing entirely. */
export type Jsonish<T = Record<string, unknown>> = T | string | null | undefined;

/** Row ids are ints today, but a uuid string must not break the client. */
export type Id = number | string;

/* -------------------------------------------------------------------------- */
/* Enumerations (contract-defined)                                             */
/* -------------------------------------------------------------------------- */

export type JobStatus = 'new' | 'shortlisted' | 'tailored' | 'applied' | 'rejected' | 'skipped';

export const JOB_STATUSES: readonly JobStatus[] = [
  'new',
  'shortlisted',
  'tailored',
  'applied',
  'rejected',
  'skipped',
] as const;

export type RunKind =
  | 'profile_import'
  | 'resume_build'
  | 'job_search'
  | 'job_tailor'
  | 'ats_score'
  | 'sandbox_exec'
  | 'full_pipeline';

export const RUN_KINDS: readonly RunKind[] = [
  'profile_import',
  'resume_build',
  'job_search',
  'job_tailor',
  'ats_score',
  'sandbox_exec',
  'full_pipeline',
] as const;

export const RUN_KIND_LABELS: Record<RunKind, string> = {
  profile_import: 'Profile import',
  resume_build: 'Resume build',
  job_search: 'Job search',
  job_tailor: 'Job tailor',
  ats_score: 'ATS score',
  sandbox_exec: 'Sandbox exec',
  full_pipeline: 'Full pipeline',
};

export type RunStatus = 'pending' | 'running' | 'done' | 'error';

/** Terminal statuses - stop polling once a run reaches one of these. */
export const RUN_TERMINAL: readonly RunStatus[] = ['done', 'error'] as const;

export type MatchVerdict = 'strong' | 'good' | 'stretch' | 'poor';

/* -------------------------------------------------------------------------- */
/* Profile                                                                     */
/* -------------------------------------------------------------------------- */

export interface Achievement {
  text: string;
  metric?: string | null;
}

/** Output of agents/profile_analyst.py -> ProfileAnalyst.analyze(). */
export interface ProfileAnalysis {
  headline?: string;
  summary?: string;
  seniority?: string;
  years_experience?: number;
  domains?: string[];
  hard_skills?: string[];
  soft_skills?: string[];
  tools?: string[];
  certifications?: string[];
  achievements?: Achievement[];
  keyword_bank?: string[];
  gaps?: string[];
  target_roles?: string[];
  positioning_statement?: string;
}

export interface Profile {
  id: Id;
  source?: string | null;
  linkedin_username?: string | null;
  headline?: string | null;
  summary?: string | null;
  raw_json?: Jsonish;
  skills_json?: Jsonish<unknown[]>;
  experience_json?: Jsonish<unknown[]>;
  education_json?: Jsonish<unknown[]>;
  analysis_json?: Jsonish<ProfileAnalysis>;
  fetched_at?: string | null;
}

/**
 * GET /api/profile. hermes-core may return the Profile row flat, or wrapped as
 * `{profile, analysis}`; `api.getProfile()` normalizes both into this shape.
 */
export interface ProfileResponse {
  profile: Profile | null;
  analysis: ProfileAnalysis | null;
}

/* -------------------------------------------------------------------------- */
/* Resume + ATS                                                                */
/* -------------------------------------------------------------------------- */

/** Weighted ATS subscores. Max values come from agents/ats.py. */
export interface AtsSubscores {
  parseability?: number;
  keyword_coverage?: number;
  contact_block?: number;
  experience_quality?: number;
  formatting?: number;
  readability?: number;
  [key: string]: number | undefined;
}

/** Maximum points each subscore can contribute (sums to 100). */
export const ATS_SUBSCORE_MAX: Record<string, number> = {
  parseability: 20,
  keyword_coverage: 25,
  contact_block: 10,
  experience_quality: 20,
  formatting: 15,
  readability: 10,
};

export const ATS_SUBSCORE_LABELS: Record<string, string> = {
  parseability: 'Parseability',
  keyword_coverage: 'Keyword coverage',
  contact_block: 'Contact block',
  experience_quality: 'Experience quality',
  formatting: 'Formatting',
  readability: 'Readability',
};

/** score_resume_deterministic() + the optional LLM semantic pass. */
export interface AtsBreakdown {
  score?: number;
  subscores?: AtsSubscores;
  matched?: string[];
  missing?: string[];
  issues?: string[];
  advice?: string[];
  /** Added by the async LLM pass in agents/ats.py -> score_resume(). */
  semantic_fit?: number | string;
  llm_issues?: string[];
  llm_advice?: string[];
}

export interface Resume {
  id: Id;
  profile_id?: Id | null;
  version?: number | null;
  label?: string | null;
  target_job_id?: Id | null;
  markdown?: string | null;
  docx_path?: string | null;
  pdf_path?: string | null;
  txt_path?: string | null;
  ats_score?: number | null;
  ats_breakdown_json?: Jsonish<AtsBreakdown>;
  created_at?: string | null;
}

export type ResumeFormat = 'docx' | 'pdf' | 'txt' | 'md';

/* -------------------------------------------------------------------------- */
/* Jobs                                                                        */
/* -------------------------------------------------------------------------- */

/** Output of agents/match_ranker.py -> MatchRanker.rank(). */
export interface MatchBreakdown {
  score?: number;
  reasons?: string[];
  matched_skills?: string[];
  missing_skills?: string[];
  verdict?: MatchVerdict;
  tailoring_notes?: string[];
}

export interface Job {
  id: Id;
  linkedin_job_id?: string | null;
  title?: string | null;
  company?: string | null;
  location?: string | null;
  url?: string | null;
  easy_apply?: boolean | null;
  posted?: string | null;
  description?: string | null;
  raw_json?: Jsonish;
  discovered_at?: string | null;
  match_score?: number | null;
  match_breakdown_json?: Jsonish<MatchBreakdown>;
  status?: JobStatus | null;
  applied_at?: string | null;
  notes?: string | null;
  tailored_resume_id?: Id | null;
}

/** POST /api/jobs/search body - mirrors the linkedin-mcp search_jobs signature. */
export interface JobSearchParams {
  keywords: string;
  location?: string;
  max_pages?: number;
  date_posted?: string;
  job_type?: string;
  experience_level?: string;
  work_type?: string;
  easy_apply?: boolean;
  sort_by?: string;
  fetch_details?: boolean;
  limit_details?: number;
}

/** GET /api/jobs query string. */
export interface JobListQuery {
  status?: JobStatus | '';
  min_score?: number;
  q?: string;
}

/* -------------------------------------------------------------------------- */
/* Runs                                                                        */
/* -------------------------------------------------------------------------- */

export interface Run {
  id: Id;
  kind?: RunKind | string;
  status?: RunStatus | string;
  params_json?: Jsonish;
  result_json?: Jsonish;
  error?: string | null;
  started_at?: string | null;
  finished_at?: string | null;
}

export type RunEventLevel = 'debug' | 'info' | 'warning' | 'error' | string;

export interface RunEvent {
  id?: Id;
  run_id?: Id;
  ts?: string | null;
  level?: RunEventLevel;
  message?: string;
}

/** A single decoded SSE frame from GET /api/runs/{id}/events. */
export interface StreamLine {
  key: string;
  ts: string;
  level: RunEventLevel;
  message: string;
}

/* -------------------------------------------------------------------------- */
/* Containers + sandbox                                                        */
/* -------------------------------------------------------------------------- */

/** One entry from SandboxManager.list_containers(). */
export interface ContainerInfo {
  id: string;
  name?: string | null;
  image?: string | null;
  status?: string | null;
  state?: string | null;
  /** Docker's port map is shape-unstable (dict of lists, or a flat list). */
  ports?: unknown;
  labels?: Record<string, string> | null;
  created?: string | null;
}

/** SandboxManager.stats(). */
export interface ContainerStats {
  cpu_percent?: number | null;
  mem_usage_mb?: number | null;
  mem_limit_mb?: number | null;
  net_rx?: number | null;
  net_tx?: number | null;
}

/** SandboxResult, as returned inside POST /api/sandbox/exec. */
export interface SandboxResult {
  exit_code?: number | null;
  stdout?: string | null;
  stderr?: string | null;
  artifacts?: string[] | null;
  container_id?: string | null;
}

/**
 * POST /api/sandbox/exec returns "Run + SandboxResult". Accept either a merged
 * object or `{run, result}`; both are handled by the Containers page.
 */
export interface SandboxExecResponse extends Run {
  run?: Run;
  result?: SandboxResult;
  exit_code?: number | null;
  stdout?: string | null;
  stderr?: string | null;
  artifacts?: string[] | null;
  container_id?: string | null;
}

/* -------------------------------------------------------------------------- */
/* Health, LLM, settings, LinkedIn                                             */
/* -------------------------------------------------------------------------- */

export interface HealthLlm {
  /** Alias of `reachable`; hermes-core emits both. Prefer `reachable ?? ok`. */
  ok?: boolean;
  reachable?: boolean;
  base_url?: string;
  key_configured?: boolean;
  primary?: string | null;
  model?: string | null;
  fallbacks?: string[] | string | null;
  models?: number | string[];
  detail?: string;
  error?: string;
}

/** LinkedInMCP.health(). */
export interface McpHealth {
  reachable?: boolean;
  authenticated?: boolean;
  detail?: string;
}

export interface Health {
  ok?: boolean;
  version?: string;
  llm?: HealthLlm;
  mcp?: McpHealth;
  docker?: boolean;
  /** Optional extras some builds of hermes-core include. */
  sandbox?: Record<string, unknown>;
  [key: string]: unknown;
}

/** One entry from GET /api/llm/models (OpenAI-compatible model object). */
export interface LlmModel {
  id: string;
  object?: string;
  owned_by?: string;
  created?: number;
  [key: string]: unknown;
}

export interface LlmTestResponse {
  model?: string;
  output?: string;
  latency_ms?: number;
}

/** GET/PUT /api/settings - the Setting(key, value) table as a flat map. */
export type SettingsMap = Record<string, string>;

export interface LinkedInLoginResponse {
  viewer_url?: string;
  instructions?: string;
}

/** Sandbox limits shown read-only on the Settings page. */
export const SANDBOX_LIMIT_KEYS: readonly string[] = [
  'hermes_sandbox_image',
  'hermes_sandbox_memory_mb',
  'hermes_sandbox_cpus',
  'hermes_sandbox_timeout_s',
  'hermes_sandbox_network',
  'hermes_sandbox_workspace',
] as const;
