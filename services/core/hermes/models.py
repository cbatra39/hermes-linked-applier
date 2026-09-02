"""SQLAlchemy 2.0 ORM models for Hermes.

ID CONVENTION (other agents must honour this):
    Every primary key is a **string UUID4 hex** (`new_id()`), EXCEPT
    `RunEvent.id` which is an autoincrementing integer because run events are an
    append-only log and monotonic ordering is what callers actually want.
    So `run_id`, `job_id`, `profile_id`, `resume_id` are all `str` in the API.

JSON COLUMNS:
    All `*_json` columns are `Text` holding a JSON string, per the build
    contract. Never touch them directly — use the paired object property added
    by `json_prop`, e.g.::

        p = Profile(source="linkedin")
        p.raw = {"firstName": "Ada"}     # dumps to p.raw_json
        p.raw                            # loads back to a dict
        p.skills = ["python", "sql"]     # p.skills_json

    Reads are defensive: a NULL/blank/corrupt column yields the empty default
    (dict/list) instead of raising, so a half-written row can never 500 the
    dashboard.
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from typing import Any, Callable, Optional

from sqlalchemy import (
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from hermes.db import Base

# --------------------------------------------------------------------------- #
# enumerations (kept as plain string constants: SQLite + easy JSON transport)
# --------------------------------------------------------------------------- #
JOB_STATUSES: tuple[str, ...] = (
    "new",
    "shortlisted",
    "tailored",
    "applied",
    "rejected",
    "skipped",
)

RUN_KINDS: tuple[str, ...] = (
    "profile_import",
    "resume_build",
    "job_search",
    "job_tailor",
    "ats_score",
    "sandbox_exec",
    "full_pipeline",
)

RUN_STATUSES: tuple[str, ...] = ("pending", "running", "done", "error")

SANDBOX_STATUSES: tuple[str, ...] = ("created", "running", "done", "timeout", "error", "killed")

PROFILE_SOURCES: tuple[str, ...] = ("linkedin", "upload", "manual")

EVENT_LEVELS: tuple[str, ...] = ("debug", "info", "warn", "error", "end")


def utcnow() -> datetime:
    """Timezone-aware UTC now (SQLite has no native tz, we normalise on read)."""
    return datetime.now(timezone.utc)


def new_id() -> str:
    """Primary-key factory: 32-char lowercase hex UUID4 (URL-safe, no dashes)."""
    return uuid.uuid4().hex


# --------------------------------------------------------------------------- #
# JSON column helper
# --------------------------------------------------------------------------- #
def json_prop(
    column_name: str,
    default_factory: Callable[[], Any] = dict,
    *,
    doc: str | None = None,
) -> property:
    """Build an object-view `property` over a `Text` JSON column.

    Args:
        column_name: the underlying mapped attribute, e.g. ``"raw_json"``.
        default_factory: what a NULL/blank/corrupt value reads back as.
        doc: optional docstring for the generated property.
    """

    def _get(self: Any) -> Any:
        raw = getattr(self, column_name, None)
        if raw is None or (isinstance(raw, str) and not raw.strip()):
            return default_factory()
        try:
            return json.loads(raw)
        except (TypeError, ValueError):
            # Corrupt payload: do not explode a GET request over it.
            return default_factory()

    def _set(self: Any, value: Any) -> None:
        if value is None:
            setattr(self, column_name, None)
            return
        if isinstance(value, str):
            # Allow assigning an already-serialised string, but validate it.
            try:
                json.loads(value)
            except (TypeError, ValueError):
                setattr(self, column_name, json.dumps(value, ensure_ascii=False))
            else:
                setattr(self, column_name, value)
            return
        setattr(
            self,
            column_name,
            json.dumps(value, ensure_ascii=False, default=str),
        )

    return property(_get, _set, None, doc or f"Object view over {column_name}.")


def json_loads_safe(raw: str | None, default: Any = None) -> Any:
    """Module-level twin of `json_prop`'s getter, for ad-hoc Text JSON reads."""
    if raw is None or (isinstance(raw, str) and not raw.strip()):
        return default
    try:
        return json.loads(raw)
    except (TypeError, ValueError):
        return default


def json_dumps_safe(value: Any) -> str | None:
    """Serialise `value` for a `*_json` Text column (None passes through)."""
    if value is None:
        return None
    return json.dumps(value, ensure_ascii=False, default=str)


# --------------------------------------------------------------------------- #
# Profile
# --------------------------------------------------------------------------- #
class Profile(Base):
    """A snapshot of the user's professional identity + its LLM analysis."""

    __tablename__ = "profile"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    source: Mapped[str] = mapped_column(String(24), default="linkedin", nullable=False)
    linkedin_username: Mapped[Optional[str]] = mapped_column(String(255), index=True)
    headline: Mapped[Optional[str]] = mapped_column(Text)
    summary: Mapped[Optional[str]] = mapped_column(Text)

    raw_json: Mapped[Optional[str]] = mapped_column(Text)
    skills_json: Mapped[Optional[str]] = mapped_column(Text)
    experience_json: Mapped[Optional[str]] = mapped_column(Text)
    education_json: Mapped[Optional[str]] = mapped_column(Text)
    analysis_json: Mapped[Optional[str]] = mapped_column(Text)

    fetched_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False, index=True
    )

    resumes: Mapped[list["Resume"]] = relationship(
        back_populates="profile",
        cascade="all, delete-orphan",
        order_by="Resume.version.desc()",
    )

    # object views over the Text JSON columns
    raw = json_prop("raw_json", dict, doc="Full scraped MCP profile payload.")
    skills = json_prop("skills_json", list, doc="Flat list of skill strings.")
    experience = json_prop("experience_json", list, doc="List of position dicts.")
    education = json_prop("education_json", list, doc="List of education dicts.")
    analysis = json_prop("analysis_json", dict, doc="ProfileAnalyst output.")

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<Profile {self.id} {self.linkedin_username!r} src={self.source}>"


# --------------------------------------------------------------------------- #
# Resume
# --------------------------------------------------------------------------- #
class Resume(Base):
    """A generated (or uploaded) resume version plus its ATS scoring."""

    __tablename__ = "resume"
    __table_args__ = (
        Index("ix_resume_profile_version", "profile_id", "version"),
    )

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    profile_id: Mapped[Optional[str]] = mapped_column(
        String(32), ForeignKey("profile.id", ondelete="CASCADE"), index=True
    )
    version: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    label: Mapped[Optional[str]] = mapped_column(String(255))

    # FK to job.id. This is the ONE real FK in the resume<->job cycle; the
    # reverse pointer (Job.tailored_resume_id) is intentionally constraint-free
    # because SQLite cannot ALTER TABLE ADD CONSTRAINT to break the cycle.
    target_job_id: Mapped[Optional[str]] = mapped_column(
        String(32), ForeignKey("job.id", ondelete="SET NULL"), index=True
    )

    markdown: Mapped[str] = mapped_column(Text, default="", nullable=False)
    docx_path: Mapped[Optional[str]] = mapped_column(Text)
    pdf_path: Mapped[Optional[str]] = mapped_column(Text)
    txt_path: Mapped[Optional[str]] = mapped_column(Text)

    ats_score: Mapped[Optional[float]] = mapped_column(Float, index=True)
    ats_breakdown_json: Mapped[Optional[str]] = mapped_column(Text)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False, index=True
    )

    profile: Mapped[Optional["Profile"]] = relationship(back_populates="resumes")
    target_job: Mapped[Optional["Job"]] = relationship(
        "Job",
        foreign_keys="Resume.target_job_id",
        back_populates="tailored_resumes",
    )

    ats_breakdown = json_prop(
        "ats_breakdown_json", dict, doc="Full score_resume() result dict."
    )

    @property
    def has_files(self) -> bool:
        """True if at least one rendered artifact path is recorded."""
        return any((self.docx_path, self.pdf_path, self.txt_path))

    def __repr__(self) -> str:  # pragma: no cover
        return f"<Resume {self.id} v{self.version} ats={self.ats_score}>"


# --------------------------------------------------------------------------- #
# Job
# --------------------------------------------------------------------------- #
class Job(Base):
    """A scouted LinkedIn job posting + its match score and workflow status.

    Hermes never applies. `url` is what the human opens to submit.
    """

    __tablename__ = "job"
    __table_args__ = (
        UniqueConstraint("linkedin_job_id", name="uq_job_linkedin_job_id"),
        Index("ix_job_status_score", "status", "match_score"),
    )

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    linkedin_job_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    title: Mapped[Optional[str]] = mapped_column(Text)
    company: Mapped[Optional[str]] = mapped_column(Text)
    location: Mapped[Optional[str]] = mapped_column(Text)
    url: Mapped[Optional[str]] = mapped_column(Text)
    easy_apply: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    posted: Mapped[Optional[str]] = mapped_column(String(128))
    description: Mapped[Optional[str]] = mapped_column(Text)
    raw_json: Mapped[Optional[str]] = mapped_column(Text)

    discovered_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False, index=True
    )

    match_score: Mapped[Optional[float]] = mapped_column(Float, index=True)
    match_breakdown_json: Mapped[Optional[str]] = mapped_column(Text)

    status: Mapped[str] = mapped_column(String(24), default="new", nullable=False, index=True)
    applied_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    notes: Mapped[Optional[str]] = mapped_column(Text)

    # Deliberately NOT a ForeignKey: job <-> resume is a cycle and SQLite has no
    # ADD CONSTRAINT, so create_all() would fail with use_alter=True. Integrity
    # is maintained in pipeline.py. Values are Resume.id strings (or NULL).
    tailored_resume_id: Mapped[Optional[str]] = mapped_column(String(32), index=True)

    tailored_resumes: Mapped[list["Resume"]] = relationship(
        "Resume",
        foreign_keys="Resume.target_job_id",
        back_populates="target_job",
    )
    tailored_resume: Mapped[Optional["Resume"]] = relationship(
        "Resume",
        primaryjoin="foreign(Job.tailored_resume_id) == Resume.id",
        viewonly=True,  # write via tailored_resume_id, read via this
        uselist=False,
    )

    raw = json_prop("raw_json", dict, doc="Raw MCP search/detail payload.")
    match_breakdown = json_prop(
        "match_breakdown_json", dict, doc="MatchRanker.rank() result dict."
    )

    @property
    def apply_url(self) -> str:
        """Canonical apply/view URL — always usable even if scraping missed it."""
        if self.url and self.url.startswith("http"):
            return self.url
        if self.url and self.url.startswith("/"):
            return "https://www.linkedin.com" + self.url
        return f"https://www.linkedin.com/jobs/view/{self.linkedin_job_id}/"

    def __repr__(self) -> str:  # pragma: no cover
        return f"<Job {self.linkedin_job_id} {self.title!r} score={self.match_score}>"


# --------------------------------------------------------------------------- #
# Run / RunEvent
# --------------------------------------------------------------------------- #
class Run(Base):
    """One background pipeline execution, streamed to the dashboard over SSE."""

    __tablename__ = "run"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    kind: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    status: Mapped[str] = mapped_column(
        String(16), default="pending", nullable=False, index=True
    )
    params_json: Mapped[Optional[str]] = mapped_column(Text)
    result_json: Mapped[Optional[str]] = mapped_column(Text)
    error: Mapped[Optional[str]] = mapped_column(Text)
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False, index=True
    )
    finished_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))

    events: Mapped[list["RunEvent"]] = relationship(
        back_populates="run",
        cascade="all, delete-orphan",
        order_by="RunEvent.id",
    )
    sandboxes: Mapped[list["Sandbox"]] = relationship(
        back_populates="run", cascade="all, delete-orphan"
    )

    params = json_prop("params_json", dict, doc="Request params the run was started with.")
    result = json_prop("result_json", dict, doc="Terminal result payload.")

    @property
    def is_terminal(self) -> bool:
        return self.status in ("done", "error")

    @property
    def duration_s(self) -> float | None:
        """Wall-clock seconds, or None while still running."""
        if not self.finished_at or not self.started_at:
            return None
        start = self.started_at
        end = self.finished_at
        # SQLite may hand back naive datetimes; assume UTC in that case.
        if start.tzinfo is None:
            start = start.replace(tzinfo=timezone.utc)
        if end.tzinfo is None:
            end = end.replace(tzinfo=timezone.utc)
        return max(0.0, (end - start).total_seconds())

    def __repr__(self) -> str:  # pragma: no cover
        return f"<Run {self.id} {self.kind} {self.status}>"


class RunEvent(Base):
    """Append-only log line for a Run.

    Integer PK on purpose: it gives a cheap monotonic cursor for SSE replay
    (`WHERE run_id=? AND id > last_id ORDER BY id`).
    """

    __tablename__ = "run_event"
    __table_args__ = (Index("ix_run_event_run_id_id", "run_id", "id"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    run_id: Mapped[str] = mapped_column(
        String(32), ForeignKey("run.id", ondelete="CASCADE"), nullable=False, index=True
    )
    ts: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False
    )
    level: Mapped[str] = mapped_column(String(16), default="info", nullable=False)
    message: Mapped[str] = mapped_column(Text, default="", nullable=False)

    run: Mapped["Run"] = relationship(back_populates="events")

    def __repr__(self) -> str:  # pragma: no cover
        return f"<RunEvent {self.run_id} {self.level} {self.message[:40]!r}>"


# --------------------------------------------------------------------------- #
# Sandbox
# --------------------------------------------------------------------------- #
class Sandbox(Base):
    """Audit record for one ephemeral sandbox container execution."""

    __tablename__ = "sandbox"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    container_id: Mapped[Optional[str]] = mapped_column(String(80), index=True)
    image: Mapped[str] = mapped_column(String(255), default="", nullable=False)
    run_id: Mapped[Optional[str]] = mapped_column(
        String(32), ForeignKey("run.id", ondelete="CASCADE"), index=True
    )
    status: Mapped[str] = mapped_column(String(16), default="created", nullable=False)
    exit_code: Mapped[Optional[int]] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False, index=True
    )
    finished_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    limits_json: Mapped[Optional[str]] = mapped_column(Text)

    run: Mapped[Optional["Run"]] = relationship(back_populates="sandboxes")

    limits = json_prop("limits_json", dict, doc="settings.sandbox_limits() snapshot.")

    def __repr__(self) -> str:  # pragma: no cover
        return f"<Sandbox {self.id} {self.status} exit={self.exit_code}>"


# --------------------------------------------------------------------------- #
# Setting (dashboard-editable key/value)
# --------------------------------------------------------------------------- #
class Setting(Base):
    """Runtime-editable configuration, overriding env defaults where read.

    Only values the dashboard is allowed to change live here (model choice, job
    search defaults...). Secrets stay in the environment.
    """

    __tablename__ = "setting"

    key: Mapped[str] = mapped_column(String(128), primary_key=True)
    value: Mapped[Optional[str]] = mapped_column(Text)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False
    )

    def __repr__(self) -> str:  # pragma: no cover
        return f"<Setting {self.key}={(self.value or '')[:32]!r}>"


__all__ = [
    "EVENT_LEVELS",
    "JOB_STATUSES",
    "PROFILE_SOURCES",
    "RUN_KINDS",
    "RUN_STATUSES",
    "SANDBOX_STATUSES",
    "Job",
    "Profile",
    "Resume",
    "Run",
    "RunEvent",
    "Sandbox",
    "Setting",
    "json_dumps_safe",
    "json_loads_safe",
    "json_prop",
    "new_id",
    "utcnow",
]
