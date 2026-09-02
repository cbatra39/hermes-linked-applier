"""Pydantic v2 request/response models mirroring the Hermes HTTP API.

Conventions every route/dashboard agent must follow:
  * All entity ids are **strings** (32-char hex UUIDs). `RunEvent.id` is an int.
  * All datetimes are timezone-aware UTC and serialise as ISO-8601 with `Z`
    offset. Rows loaded from SQLite may be naive; the `from_model` helpers below
    normalise them, so prefer those over `Model.model_validate(orm_row)` when a
    naive datetime or a `*_json` column is involved.
  * The `*Out` models carry `from_model()` classmethods that already unpack the
    `*_json` Text columns into real objects. They are duck-typed (`getattr`), so
    this module does not import `hermes.models` at runtime and stays free of
    import cycles.
  * Every POST that kicks off background work returns `RunOut`; the dashboard
    then follows `GET /api/runs/{id}/events` (SSE) for progress.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator

from hermes import __version__

# --------------------------------------------------------------------------- #
# shared aliases
# --------------------------------------------------------------------------- #
JobStatus = Literal["new", "shortlisted", "tailored", "applied", "rejected", "skipped"]
RunKind = Literal[
    "profile_import",
    "resume_build",
    "job_search",
    "job_tailor",
    "ats_score",
    "sandbox_exec",
    "full_pipeline",
]
RunStatus = Literal["pending", "running", "done", "error"]
ResumeFormat = Literal["docx", "pdf", "txt", "md"]
MatchVerdict = Literal["strong", "good", "stretch", "poor"]


def _aware(dt: Any) -> Optional[datetime]:
    """Coerce a possibly-naive SQLite datetime to timezone-aware UTC."""
    if dt is None:
        return None
    if isinstance(dt, datetime):
        return dt if dt.tzinfo is not None else dt.replace(tzinfo=timezone.utc)
    return None


class HermesModel(BaseModel):
    """Base: tolerant of ORM objects, strips surrounding whitespace on strings."""

    model_config = ConfigDict(from_attributes=True, str_strip_whitespace=True)


# --------------------------------------------------------------------------- #
# generic
# --------------------------------------------------------------------------- #
class OkResponse(HermesModel):
    """Trivial acknowledgement for side-effecting endpoints."""

    ok: bool = True
    detail: Optional[str] = None


class ErrorResponse(HermesModel):
    """Uniform error envelope raised through the FastAPI exception handlers."""

    ok: bool = False
    error: str
    detail: Optional[str] = None


# --------------------------------------------------------------------------- #
# health  ->  GET /api/health
# --------------------------------------------------------------------------- #
class LLMHealth(HermesModel):
    configured: bool = False
    reachable: bool = False
    base_url: str = ""
    model_count: int = 0
    primary: Optional[str] = None
    fallbacks: list[str] = Field(default_factory=list)
    detail: str = ""


class MCPHealth(HermesModel):
    """Mirrors `LinkedInMCP.health()`."""

    reachable: bool = False
    authenticated: bool = False
    detail: str = ""


class HealthResponse(HermesModel):
    ok: bool = True
    version: str = __version__
    llm: LLMHealth = Field(default_factory=LLMHealth)
    mcp: MCPHealth = Field(default_factory=MCPHealth)
    docker: bool = False
    db: dict[str, Any] = Field(default_factory=dict)
    config: dict[str, Any] = Field(default_factory=dict)


# --------------------------------------------------------------------------- #
# settings  ->  GET/PUT /api/settings
# --------------------------------------------------------------------------- #
class SettingOut(HermesModel):
    key: str
    value: Optional[str] = None
    updated_at: Optional[datetime] = None

    @classmethod
    def from_model(cls, row: Any) -> "SettingOut":
        return cls(
            key=getattr(row, "key"),
            value=getattr(row, "value", None),
            updated_at=_aware(getattr(row, "updated_at", None)),
        )


class SettingsResponse(HermesModel):
    """Editable rows plus a flat `values` map for convenient dashboard binding."""

    items: list[SettingOut] = Field(default_factory=list)
    values: dict[str, Optional[str]] = Field(default_factory=dict)
    # Read-only environment facts shown on the Settings page (never secrets).
    env: dict[str, Any] = Field(default_factory=dict)
    sandbox_limits: dict[str, Any] = Field(default_factory=dict)


class SettingsUpdate(HermesModel):
    """PUT body. Unknown keys are rejected by the route, not here."""

    values: dict[str, Optional[str]] = Field(default_factory=dict)


# --------------------------------------------------------------------------- #
# llm  ->  GET /api/llm/models, POST /api/llm/test
# --------------------------------------------------------------------------- #
class ModelInfo(HermesModel):
    """One entry from the router's `GET /v1/models` (extra keys preserved)."""

    model_config = ConfigDict(from_attributes=True, extra="allow")

    id: str
    owned_by: Optional[str] = None
    object: Optional[str] = None


class ModelsResponse(HermesModel):
    models: list[ModelInfo] = Field(default_factory=list)
    primary: Optional[str] = None
    fallbacks: list[str] = Field(default_factory=list)
    detail: str = ""


class LLMTestRequest(HermesModel):
    prompt: str = Field(min_length=1, max_length=8000)
    model: Optional[str] = None
    temperature: float = Field(default=0.2, ge=0.0, le=2.0)
    max_tokens: Optional[int] = Field(default=None, ge=1, le=32000)


class LLMTestResponse(HermesModel):
    model: str
    output: str
    latency_ms: int


# --------------------------------------------------------------------------- #
# linkedin  ->  GET /api/linkedin/status, POST /api/linkedin/login
# --------------------------------------------------------------------------- #
class LinkedInStatus(MCPHealth):
    """`GET /api/linkedin/status` — same shape as mcp.health()."""

    url: str = ""


class LinkedInLoginResponse(HermesModel):
    viewer_url: str
    instructions: str


# --------------------------------------------------------------------------- #
# profile
# --------------------------------------------------------------------------- #
class ProfileImportRequest(HermesModel):
    """POST /api/profile/import — omit the username to scrape the logged-in user."""

    linkedin_username: Optional[str] = None

    @field_validator("linkedin_username", mode="before")
    @classmethod
    def _clean_username(cls, v: Any) -> Any:
        """Accept a full profile URL and reduce it to the vanity slug."""
        if not isinstance(v, str):
            return v
        s = v.strip().strip("/")
        if not s:
            return None
        if "linkedin.com" in s:
            parts = [p for p in s.split("/") if p]
            if "in" in parts:
                idx = parts.index("in")
                if idx + 1 < len(parts):
                    return parts[idx + 1].split("?")[0]
            return parts[-1].split("?")[0]
        return s


class Achievement(HermesModel):
    text: str = ""
    metric: Optional[str] = None


class ProfileAnalysis(HermesModel):
    """Exact shape returned by `ProfileAnalyst.analyze()`."""

    model_config = ConfigDict(from_attributes=True, extra="allow")

    headline: str = ""
    summary: str = ""
    seniority: str = ""
    years_experience: float = 0.0
    domains: list[str] = Field(default_factory=list)
    hard_skills: list[str] = Field(default_factory=list)
    soft_skills: list[str] = Field(default_factory=list)
    tools: list[str] = Field(default_factory=list)
    certifications: list[str] = Field(default_factory=list)
    achievements: list[Achievement] = Field(default_factory=list)
    keyword_bank: list[str] = Field(default_factory=list)
    gaps: list[str] = Field(default_factory=list)
    target_roles: list[str] = Field(default_factory=list)
    positioning_statement: str = ""


class ProfileOut(HermesModel):
    id: str
    source: str = "linkedin"
    linkedin_username: Optional[str] = None
    headline: Optional[str] = None
    summary: Optional[str] = None
    skills: list[Any] = Field(default_factory=list)
    experience: list[Any] = Field(default_factory=list)
    education: list[Any] = Field(default_factory=list)
    analysis: dict[str, Any] = Field(default_factory=dict)
    fetched_at: Optional[datetime] = None
    resume_count: int = 0

    @classmethod
    def from_model(cls, row: Any, *, resume_count: int = 0) -> "ProfileOut":
        return cls(
            id=getattr(row, "id"),
            source=getattr(row, "source", "linkedin") or "linkedin",
            linkedin_username=getattr(row, "linkedin_username", None),
            headline=getattr(row, "headline", None),
            summary=getattr(row, "summary", None),
            skills=list(getattr(row, "skills", []) or []),
            experience=list(getattr(row, "experience", []) or []),
            education=list(getattr(row, "education", []) or []),
            analysis=dict(getattr(row, "analysis", {}) or {}),
            fetched_at=_aware(getattr(row, "fetched_at", None)),
            resume_count=resume_count,
        )


class ProfileResponse(HermesModel):
    """GET /api/profile — latest profile (may be absent on a fresh install)."""

    profile: Optional[ProfileOut] = None
    raw: dict[str, Any] = Field(default_factory=dict)
    detail: str = ""


# --------------------------------------------------------------------------- #
# ATS scoring
# --------------------------------------------------------------------------- #
class AtsSubscores(HermesModel):
    """Weighted subscores; the six weights sum to 100."""

    model_config = ConfigDict(from_attributes=True, extra="allow")

    parseability: float = 0.0  # max 20
    keyword_coverage: float = 0.0  # max 25
    contact_block: float = 0.0  # max 10
    experience_quality: float = 0.0  # max 20
    formatting: float = 0.0  # max 15
    readability: float = 0.0  # max 10


class AtsResult(HermesModel):
    """`score_resume_deterministic()` + optional LLM semantic pass.

    HONEST CAVEAT (surface this in the UI): this is a heuristic proxy for how a
    generic ATS parser is likely to treat the document. It is NOT any specific
    vendor's parser and guarantees no particular score in Workday/Greenhouse/etc.
    """

    model_config = ConfigDict(from_attributes=True, extra="allow")

    score: float = 0.0
    subscores: AtsSubscores = Field(default_factory=AtsSubscores)
    matched: list[str] = Field(default_factory=list)
    missing: list[str] = Field(default_factory=list)
    issues: list[str] = Field(default_factory=list)
    advice: list[str] = Field(default_factory=list)
    # present only after the async LLM pass
    semantic_fit: Optional[float] = None
    llm_issues: list[str] = Field(default_factory=list)
    llm_advice: list[str] = Field(default_factory=list)


# --------------------------------------------------------------------------- #
# resumes
# --------------------------------------------------------------------------- #
class ResumeGenerateRequest(HermesModel):
    """POST /api/resume/generate — both ids optional (latest profile is used)."""

    profile_id: Optional[str] = None
    target_job_id: Optional[str] = None
    label: Optional[str] = None


class ResumeScoreRequest(HermesModel):
    """POST /api/resume/score — job_id supplies the JD for keyword coverage."""

    resume_id: str
    job_id: Optional[str] = None


class ResumeUploadResponse(HermesModel):
    """POST /api/resume/upload — text is extracted and stored, not scored."""

    filename: str
    format: str
    chars: int
    stored_path: str
    preview: str = ""
    profile_id: Optional[str] = None
    resume_id: Optional[str] = None


class ResumeOut(HermesModel):
    id: str
    profile_id: Optional[str] = None
    version: int = 1
    label: Optional[str] = None
    target_job_id: Optional[str] = None
    markdown: str = ""
    docx_path: Optional[str] = None
    pdf_path: Optional[str] = None
    txt_path: Optional[str] = None
    ats_score: Optional[float] = None
    ats_breakdown: dict[str, Any] = Field(default_factory=dict)
    created_at: Optional[datetime] = None
    # convenience for the dashboard's download buttons
    available_formats: list[str] = Field(default_factory=list)

    @classmethod
    def from_model(cls, row: Any, *, include_markdown: bool = True) -> "ResumeOut":
        formats: list[str] = ["md"]
        for fmt, attr in (("docx", "docx_path"), ("pdf", "pdf_path"), ("txt", "txt_path")):
            if getattr(row, attr, None):
                formats.append(fmt)
        return cls(
            id=getattr(row, "id"),
            profile_id=getattr(row, "profile_id", None),
            version=int(getattr(row, "version", 1) or 1),
            label=getattr(row, "label", None),
            target_job_id=getattr(row, "target_job_id", None),
            markdown=(getattr(row, "markdown", "") or "") if include_markdown else "",
            docx_path=getattr(row, "docx_path", None),
            pdf_path=getattr(row, "pdf_path", None),
            txt_path=getattr(row, "txt_path", None),
            ats_score=getattr(row, "ats_score", None),
            ats_breakdown=dict(getattr(row, "ats_breakdown", {}) or {}),
            created_at=_aware(getattr(row, "created_at", None)),
            available_formats=formats,
        )


class ResumeListResponse(HermesModel):
    items: list[ResumeOut] = Field(default_factory=list)
    total: int = 0


# --------------------------------------------------------------------------- #
# jobs
# --------------------------------------------------------------------------- #
class JobSearchRequest(HermesModel):
    """POST /api/jobs/search — mirrors the MCP `search_jobs` tool signature."""

    keywords: str = Field(min_length=1, max_length=500)
    location: Optional[str] = None
    max_pages: int = Field(default=3, ge=1, le=10)
    date_posted: Optional[str] = None  # e.g. "past-24h" | "past-week" | "past-month"
    job_type: Optional[str] = None  # full-time | part-time | contract | ...
    experience_level: Optional[str] = None  # internship | entry | associate | ...
    work_type: Optional[str] = None  # on-site | remote | hybrid
    easy_apply: bool = False
    sort_by: Optional[str] = None  # relevance | date
    # enrichment (Hermes-side, not MCP args)
    fetch_details: bool = True
    limit_details: int = Field(default=25, ge=0, le=200)
    rank: bool = True  # score results against the stored profile analysis


class MatchBreakdown(HermesModel):
    """Exact shape returned by `MatchRanker.rank()`."""

    model_config = ConfigDict(from_attributes=True, extra="allow")

    score: float = 0.0
    reasons: list[str] = Field(default_factory=list)
    matched_skills: list[str] = Field(default_factory=list)
    missing_skills: list[str] = Field(default_factory=list)
    verdict: MatchVerdict = "poor"
    tailoring_notes: list[str] = Field(default_factory=list)


class JobOut(HermesModel):
    id: str
    linkedin_job_id: str
    title: Optional[str] = None
    company: Optional[str] = None
    location: Optional[str] = None
    url: Optional[str] = None
    apply_url: str = ""
    easy_apply: bool = False
    posted: Optional[str] = None
    description: Optional[str] = None
    discovered_at: Optional[datetime] = None
    match_score: Optional[float] = None
    match_breakdown: dict[str, Any] = Field(default_factory=dict)
    status: JobStatus = "new"
    applied_at: Optional[datetime] = None
    notes: Optional[str] = None
    tailored_resume_id: Optional[str] = None

    @classmethod
    def from_model(cls, row: Any, *, include_description: bool = True) -> "JobOut":
        desc = getattr(row, "description", None)
        return cls(
            id=getattr(row, "id"),
            linkedin_job_id=getattr(row, "linkedin_job_id", "") or "",
            title=getattr(row, "title", None),
            company=getattr(row, "company", None),
            location=getattr(row, "location", None),
            url=getattr(row, "url", None),
            apply_url=getattr(row, "apply_url", None) or (getattr(row, "url", "") or ""),
            easy_apply=bool(getattr(row, "easy_apply", False)),
            posted=getattr(row, "posted", None),
            description=desc if include_description else None,
            discovered_at=_aware(getattr(row, "discovered_at", None)),
            match_score=getattr(row, "match_score", None),
            match_breakdown=dict(getattr(row, "match_breakdown", {}) or {}),
            status=getattr(row, "status", "new") or "new",
            applied_at=_aware(getattr(row, "applied_at", None)),
            notes=getattr(row, "notes", None),
            tailored_resume_id=getattr(row, "tailored_resume_id", None),
        )


class JobListResponse(HermesModel):
    items: list[JobOut] = Field(default_factory=list)
    total: int = 0


class JobUpdate(HermesModel):
    """PATCH /api/jobs/{id} — only the provided fields change."""

    status: Optional[JobStatus] = None
    notes: Optional[str] = None


# --------------------------------------------------------------------------- #
# runs
# --------------------------------------------------------------------------- #
class RunEventOut(HermesModel):
    id: int
    run_id: str
    ts: Optional[datetime] = None
    level: str = "info"
    message: str = ""

    @classmethod
    def from_model(cls, row: Any) -> "RunEventOut":
        return cls(
            id=int(getattr(row, "id")),
            run_id=getattr(row, "run_id"),
            ts=_aware(getattr(row, "ts", None)),
            level=getattr(row, "level", "info") or "info",
            message=getattr(row, "message", "") or "",
        )


class RunOut(HermesModel):
    id: str
    kind: RunKind
    status: RunStatus = "pending"
    params: dict[str, Any] = Field(default_factory=dict)
    result: dict[str, Any] = Field(default_factory=dict)
    error: Optional[str] = None
    started_at: Optional[datetime] = None
    finished_at: Optional[datetime] = None
    duration_s: Optional[float] = None
    events: list[RunEventOut] = Field(default_factory=list)

    @classmethod
    def from_model(cls, row: Any, *, events: Optional[list[Any]] = None) -> "RunOut":
        return cls(
            id=getattr(row, "id"),
            kind=getattr(row, "kind"),
            status=getattr(row, "status", "pending") or "pending",
            params=dict(getattr(row, "params", {}) or {}),
            result=dict(getattr(row, "result", {}) or {}),
            error=getattr(row, "error", None),
            started_at=_aware(getattr(row, "started_at", None)),
            finished_at=_aware(getattr(row, "finished_at", None)),
            duration_s=getattr(row, "duration_s", None),
            events=[RunEventOut.from_model(e) for e in (events or [])],
        )


class RunListResponse(HermesModel):
    items: list[RunOut] = Field(default_factory=list)
    total: int = 0


# --------------------------------------------------------------------------- #
# containers  ->  /api/containers*
# --------------------------------------------------------------------------- #
class ContainerPort(HermesModel):
    ip: Optional[str] = None
    private_port: Optional[int] = None
    public_port: Optional[int] = None
    type: Optional[str] = None


class ContainerOut(HermesModel):
    id: str
    name: str = ""
    image: str = ""
    status: str = ""
    state: str = ""
    ports: list[ContainerPort] = Field(default_factory=list)
    labels: dict[str, str] = Field(default_factory=dict)
    created: Optional[str] = None
    hermes_role: Optional[str] = None


class ContainerListResponse(HermesModel):
    items: list[ContainerOut] = Field(default_factory=list)
    docker_ok: bool = True
    detail: str = ""


class ContainerStats(HermesModel):
    id: str = ""
    cpu_percent: float = 0.0
    mem_usage_mb: float = 0.0
    mem_limit_mb: float = 0.0
    net_rx: float = 0.0
    net_tx: float = 0.0


# --------------------------------------------------------------------------- #
# sandbox  ->  POST /api/sandbox/exec
# --------------------------------------------------------------------------- #
class SandboxExecRequest(HermesModel):
    code: str = Field(min_length=1, max_length=200_000)
    files: Optional[dict[str, str]] = None  # relative path -> text content
    timeout: Optional[int] = Field(default=None, ge=1, le=3600)
    network: Optional[str] = None  # "none" (default) or a compose network name


class SandboxResultOut(HermesModel):
    """Serialised `SandboxResult` dataclass from hermes/sandbox.py."""

    exit_code: int = -1
    stdout: str = ""
    stderr: str = ""
    artifacts: list[str] = Field(default_factory=list)
    container_id: str = ""


class SandboxExecResponse(HermesModel):
    run: RunOut
    result: Optional[SandboxResultOut] = None


# --------------------------------------------------------------------------- #
# rendering  ->  render.render_resume()
# --------------------------------------------------------------------------- #
class RenderResult(HermesModel):
    docx: Optional[str] = None
    txt: Optional[str] = None
    pdf: Optional[str] = None


__all__ = [
    "Achievement",
    "AtsResult",
    "AtsSubscores",
    "ContainerListResponse",
    "ContainerOut",
    "ContainerPort",
    "ContainerStats",
    "ErrorResponse",
    "HealthResponse",
    "HermesModel",
    "JobListResponse",
    "JobOut",
    "JobSearchRequest",
    "JobStatus",
    "JobUpdate",
    "LLMHealth",
    "LLMTestRequest",
    "LLMTestResponse",
    "LinkedInLoginResponse",
    "LinkedInStatus",
    "MCPHealth",
    "MatchBreakdown",
    "MatchVerdict",
    "ModelInfo",
    "ModelsResponse",
    "OkResponse",
    "ProfileAnalysis",
    "ProfileImportRequest",
    "ProfileOut",
    "ProfileResponse",
    "RenderResult",
    "ResumeFormat",
    "ResumeGenerateRequest",
    "ResumeListResponse",
    "ResumeOut",
    "ResumeScoreRequest",
    "ResumeUploadResponse",
    "RunEventOut",
    "RunKind",
    "RunListResponse",
    "RunOut",
    "RunStatus",
    "SandboxExecRequest",
    "SandboxExecResponse",
    "SandboxResultOut",
    "SettingOut",
    "SettingsResponse",
    "SettingsUpdate",
]
