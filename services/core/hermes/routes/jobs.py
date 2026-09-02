"""``/api/jobs*`` — scout, browse, triage and tailor for LinkedIn job postings.

**Hermes never submits an application.** There is no apply/submit tool on the
LinkedIn MCP server (19 tools, verified — none of them apply), so this module
deliberately exposes no such endpoint. What it does is: run a search, rank the
results against the stored profile analysis, let the human triage them with
``PATCH /api/jobs/{id}`` and build a tailored resume for one of them. The
``apply_url`` in every job payload is the link the *human* clicks.

Filter names on ``POST /api/jobs/search`` mirror the MCP ``search_jobs`` tool
signature exactly (``keywords`` is required; ``location``, ``max_pages``,
``date_posted``, ``job_type``, ``experience_level``, ``work_type``,
``easy_apply``, ``sort_by``), plus the Hermes-side enrichment flags
``fetch_details`` / ``limit_details``.
"""

from __future__ import annotations

import logging
from typing import Any, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from hermes.db import get_db
from hermes.models import Job
from hermes.routes._common import (
    JOB_STATUSES,
    coerce_pk,
    csv_list,
    job_dict,
    run_dict,
    utcnow,
)
from hermes.runner import UnknownRunKind, start_run
from hermes.schemas import JobSearchRequest, JobUpdate

log = logging.getLogger("hermes.api.jobs")

router = APIRouter(tags=["jobs"])

#: Columns a free-text ``?q=`` searches.
_SEARCHABLE = ("title", "company", "location", "description")


# --------------------------------------------------------------------------- #
# local helpers (self-contained per route module, matching routes/health.py)
# --------------------------------------------------------------------------- #


def _run_response(run: Any) -> dict[str, Any]:
    """``run_dict`` plus the ``*_json`` aliases the dashboard client reads."""
    payload = run_dict(run)
    payload["params_json"] = payload.get("params")
    payload["result_json"] = payload.get("result")
    return payload


def _start(db: Session, kind: str, params: dict[str, Any]) -> dict[str, Any]:
    """Schedule a run, translating runner failures into honest HTTP errors."""
    try:
        run = start_run(db, kind, params)
    except UnknownRunKind as exc:  # pragma: no cover - guarded by RUN_KINDS
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(
            status_code=422, detail=f"Cannot start the {kind} run: {exc}"
        ) from exc
    except RuntimeError as exc:
        raise HTTPException(
            status_code=500, detail=f"Cannot start the {kind} run: {exc}"
        ) from exc
    return _run_response(run)


def _job_response(job: Job, *, full: bool) -> dict[str, Any]:
    """``job_dict`` plus the aliases the dashboard client reads.

    ``src/lib/types.ts::Job`` expects the raw column names
    (``match_breakdown_json``, ``raw_json``) read through ``parseJsonish``,
    while ``_common.job_dict`` emits ``match_breakdown`` / ``raw``. Both
    spellings are emitted. ``apply_url`` comes from the model property so the
    UI always has a working link even when scraping missed ``url``.
    """
    payload = job_dict(job, full=full)
    payload["apply_url"] = getattr(job, "apply_url", None) or payload.get("url") or ""
    breakdown = payload.get("match_breakdown")
    if breakdown is None:
        breakdown = dict(getattr(job, "match_breakdown", {}) or {})
    payload["match_breakdown"] = breakdown
    payload["match_breakdown_json"] = breakdown
    if full:
        payload["raw_json"] = payload.get("raw")
    return payload


def _get_job_or_404(db: Session, job_id: str) -> Job:
    job = db.get(Job, coerce_pk(Job, job_id))
    if job is None:
        raise HTTPException(
            status_code=404,
            detail=(
                f"Job {job_id!r} not found. GET /api/jobs lists the jobs Hermes has "
                "scouted; run POST /api/jobs/search to find more."
            ),
        )
    return job


def _like_pattern(needle: str) -> str:
    """A ``LIKE`` pattern with the wildcards in the user's text escaped."""
    escaped = needle.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{escaped}%"


def _validated_statuses(status: Optional[str]) -> list[str]:
    """Parse ``?status=`` (single value or comma-separated) against the domain."""
    wanted = csv_list(status)
    unknown = [s for s in wanted if s not in JOB_STATUSES]
    if unknown:
        raise HTTPException(
            status_code=422,
            detail=(
                f"Unknown job status {', '.join(repr(s) for s in unknown)}. "
                f"Valid values: {', '.join(JOB_STATUSES)}."
            ),
        )
    return wanted


# --------------------------------------------------------------------------- #
# POST /jobs/search
# --------------------------------------------------------------------------- #


@router.post("/jobs/search", summary="Search LinkedIn jobs, store and rank them")
async def search_jobs(
    payload: JobSearchRequest,
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    """Kick off a ``job_search`` run.

    Returns the Run immediately; follow ``GET /api/runs/{id}/events`` for
    progress. Results are upserted by ``linkedin_job_id`` (re-running a search
    updates existing rows rather than duplicating them) and then scored against
    the stored profile analysis.

    If LinkedIn is not connected the run fails with the MCP's own "No valid
    LinkedIn session" message — the dashboard shows that as "LinkedIn not
    connected" with a link to the login flow.
    """
    params = payload.model_dump(exclude_none=True)
    log.info(
        "job search requested: keywords=%r location=%r max_pages=%s",
        params.get("keywords"),
        params.get("location"),
        params.get("max_pages"),
    )
    return _start(db, "job_search", params)


# --------------------------------------------------------------------------- #
# GET /jobs, GET /jobs/{id}
# --------------------------------------------------------------------------- #


@router.get("/jobs", summary="Browse scouted jobs, best match first")
def list_jobs(
    status: Optional[str] = Query(
        None,
        description=(
            "Filter by workflow status. One value, or several comma-separated: "
            + ", ".join(JOB_STATUSES)
        ),
    ),
    min_score: Optional[float] = Query(
        None, ge=0, le=100, description="Only jobs whose match_score is >= this."
    ),
    q: Optional[str] = Query(
        None,
        max_length=200,
        description="Free text over title, company, location and description.",
    ),
    limit: int = Query(100, ge=1, le=500),
    offset: int = Query(0, ge=0),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    """Jobs ordered by ``match_score`` DESC (NULLs last), then newest first.

    Unranked jobs sort last rather than first: a NULL score means "not scored
    yet", not "scored zero", and burying them keeps the top of the list useful
    while a search is still ranking.
    """
    filters = []
    statuses = _validated_statuses(status)
    if statuses:
        filters.append(Job.status.in_(statuses))
    if min_score is not None:
        filters.append(Job.match_score.is_not(None))
        filters.append(Job.match_score >= float(min_score))

    needle = (q or "").strip()
    if needle:
        pattern = _like_pattern(needle)
        filters.append(
            or_(
                *[
                    getattr(Job, column).ilike(pattern, escape="\\")
                    for column in _SEARCHABLE
                ]
            )
        )

    total = int(
        db.execute(select(func.count()).select_from(Job).where(*filters)).scalar() or 0
    )
    rows = (
        db.execute(
            select(Job)
            .where(*filters)
            # `is_(None).asc()` puts scored rows first on every backend; plain
            # `NULLS LAST` is not portable to older SQLite builds.
            .order_by(
                Job.match_score.is_(None).asc(),
                Job.match_score.desc(),
                Job.discovered_at.desc(),
                Job.id.desc(),
            )
            .limit(limit)
            .offset(offset)
        )
        .scalars()
        .all()
    )
    return {
        "items": [_job_response(row, full=False) for row in rows],
        "total": total,
        "limit": limit,
        "offset": offset,
    }


@router.get("/jobs/{job_id}", summary="One job, with its full description + ranking")
def get_job(job_id: str, db: Session = Depends(get_db)) -> dict[str, Any]:
    return _job_response(_get_job_or_404(db, job_id), full=True)


# --------------------------------------------------------------------------- #
# PATCH /jobs/{id}
# --------------------------------------------------------------------------- #


@router.patch("/jobs/{job_id}", summary="Update a job's status / notes")
def patch_job(
    job_id: str,
    payload: JobUpdate,
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    """Triage one job.

    ``status`` must be one of ``new | shortlisted | tailored | applied |
    rejected | skipped``. Moving a job to ``applied`` stamps ``applied_at`` —
    that is the human telling Hermes "I clicked Apply myself"; Hermes never
    submits anything, so nothing else sets this field.
    """
    if payload.status is None and payload.notes is None:
        raise HTTPException(
            status_code=422,
            detail=(
                "Nothing to update. Send {\"status\": ...} and/or {\"notes\": ...}. "
                f"Valid statuses: {', '.join(JOB_STATUSES)}."
            ),
        )

    job = _get_job_or_404(db, job_id)

    if payload.status is not None:
        new_status = str(payload.status)
        if new_status not in JOB_STATUSES:  # pragma: no cover - schema Literal guards
            raise HTTPException(
                status_code=422,
                detail=(
                    f"Invalid status {new_status!r}. Valid values: "
                    f"{', '.join(JOB_STATUSES)}."
                ),
            )
        previous = job.status
        job.status = new_status
        if new_status == "applied" and previous != "applied" and job.applied_at is None:
            job.applied_at = utcnow()
        log.info("job %s status %s -> %s", job.id, previous, new_status)

    if payload.notes is not None:
        job.notes = payload.notes

    db.commit()
    db.refresh(job)
    return _job_response(job, full=True)


# --------------------------------------------------------------------------- #
# POST /jobs/{id}/tailor
# --------------------------------------------------------------------------- #


@router.post("/jobs/{job_id}/tailor", summary="Build a resume tailored to one job")
async def tailor_job(job_id: str, db: Session = Depends(get_db)) -> dict[str, Any]:
    """Kick off a ``job_tailor`` run for this posting.

    The run builds a job-specific resume, scores it against that job
    description, links it as ``Job.tailored_resume_id`` and moves the job to
    ``status="tailored"``. **No application is submitted** — when it finishes,
    download the resume and open ``apply_url`` yourself.
    """
    job = _get_job_or_404(db, job_id)
    if not (job.description or "").strip():
        log.info(
            "job %s has no scraped description; tailoring will use title/company only",
            job.id,
        )
    return _start(db, "job_tailor", {"job_id": str(job.id)})
