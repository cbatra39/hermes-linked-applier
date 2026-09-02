"""``/api/profile`` — import a LinkedIn profile and read the stored snapshot.

``POST /api/profile/import`` is deliberately fire-and-forget: it creates a Run
row, schedules ``pipeline.run_profile_import`` and hands the dashboard a run id
to follow over ``GET /api/runs/{id}/events``. A scrape plus the LLM analysis
takes 20-90s — far longer than an HTTP request should stay open, and the whole
point of the SSE log drawer.

``GET /api/profile`` returns the most recent Profile *with* its analysis. On a
fresh install it answers ``404`` with the exact next step instead of an empty
``{"profile": null}`` body, so the dashboard can render a call to action rather
than a blank page.

Response shape note (see the build report): ``_common.profile_dict`` emits the
parsed ``analysis`` object while the dashboard client
(``src/lib/api.ts::normalizeProfile``) reads a top-level ``analysis`` *or*
``profile.analysis_json``. Both spellings are emitted here so neither consumer
has to guess; the payload is otherwise exactly ``profile_dict``.
"""

from __future__ import annotations

import logging
from typing import Any, Optional

from fastapi import APIRouter, Body, Depends, HTTPException, Query
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from hermes.db import get_db
from hermes.models import Profile, Resume
from hermes.routes._common import profile_dict, run_dict
from hermes.runner import UnknownRunKind, start_run
from hermes.schemas import ProfileImportRequest

log = logging.getLogger("hermes.api.profile")

router = APIRouter(tags=["profile"])


# --------------------------------------------------------------------------- #
# local helpers (kept per-module so each route file stays self-contained,
# matching routes/health.py)
# --------------------------------------------------------------------------- #


def _run_response(run: Any) -> dict[str, Any]:
    """``run_dict`` plus the ``*_json`` aliases the dashboard client reads.

    ``services/dashboard/src/lib/types.ts`` models a Run with ``params_json`` /
    ``result_json`` (raw column names, read through ``parseJsonish``), while
    ``_common.run_dict`` emits ``params`` / ``result``. Emitting both keys keeps
    every consumer correct without forking the shared serialiser.
    """
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
    except RuntimeError as exc:  # no running event loop / DB write failure
        raise HTTPException(
            status_code=500, detail=f"Cannot start the {kind} run: {exc}"
        ) from exc
    return _run_response(run)


def _latest_profile(db: Session) -> Optional[Profile]:
    """Most recently fetched Profile row (``None`` on a fresh install)."""
    return (
        db.execute(
            select(Profile)
            .order_by(Profile.fetched_at.desc(), Profile.id.desc())
            .limit(1)
        )
        .scalars()
        .first()
    )


# --------------------------------------------------------------------------- #
# routes
# --------------------------------------------------------------------------- #


@router.post("/profile/import", summary="Scrape + analyse a LinkedIn profile")
async def import_profile(
    payload: Optional[ProfileImportRequest] = Body(default=None),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    """Kick off a ``profile_import`` run.

    Omit ``linkedin_username`` to import the *logged-in* LinkedIn user via the
    MCP ``get_my_profile`` tool; pass a vanity slug (or a full profile URL — the
    request model reduces it to the slug) to import someone else's public
    profile.

    Returns the Run immediately. Follow ``GET /api/runs/{id}/events`` for
    progress. If LinkedIn is not connected the run fails with the MCP's own
    "No valid LinkedIn session" message; the dashboard surfaces that as
    "LinkedIn not connected".
    """
    username = (payload.linkedin_username if payload else None) or None
    params: dict[str, Any] = {}
    if username:
        params["linkedin_username"] = username
    log.info("profile import requested (username=%s)", username or "<self>")
    return _start(db, "profile_import", params)


@router.get("/profile", summary="Latest stored profile + its analysis")
def get_profile(
    include_raw: bool = Query(
        False,
        description="Include the full scraped MCP payload (large; off by default).",
    ),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    """The most recent Profile snapshot, or 404 with the next step to take."""
    profile = _latest_profile(db)
    if profile is None:
        raise HTTPException(
            status_code=404,
            detail=(
                "No LinkedIn profile has been imported yet. POST /api/profile/import "
                "(LinkedIn page → \"Import profile\") to scrape and analyse one. "
                "That call needs a logged-in LinkedIn session — run "
                "scripts/linkedin-login.ps1 (or .sh) first if GET /api/linkedin/status "
                "reports authenticated=false."
            ),
        )

    payload = profile_dict(profile, full=include_raw)
    analysis = payload.get("analysis") or {}
    # Alias for the dashboard client, which reads `analysis_json` off the row.
    payload["analysis_json"] = payload.get("analysis")

    resume_count = int(
        db.execute(
            select(func.count())
            .select_from(Resume)
            .where(Resume.profile_id == profile.id)
        ).scalar()
        or 0
    )
    payload["resume_count"] = resume_count

    if analysis:
        detail = ""
    else:
        detail = (
            "This profile was stored without an LLM analysis, so resume generation "
            "will refuse to run. Re-import it (POST /api/profile/import) once "
            "GET /api/health reports the LLM router as reachable."
        )

    return {
        "profile": payload,
        "analysis": analysis,
        "raw": payload.get("raw", {}) if include_raw else {},
        "resume_count": resume_count,
        "detail": detail,
    }
