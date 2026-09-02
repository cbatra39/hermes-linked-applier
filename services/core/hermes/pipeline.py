"""Hermes orchestration pipelines.

Each ``run_*`` coroutine is the body of one ``Run`` row: it flips the row to
``running``, streams progress through the event bus, persists domain rows, and
finishes by writing ``result_json`` (status ``done``) or ``error`` (status
``error``). No pipeline is ever allowed to raise into its caller —
:func:`_run_scope` converts every exception into a recorded failure — so a
background task can never die silently.

Agent wiring (contract order)::

    ProfileAnalyst -> ResumeArchitect -> render_resume -> ats.score_resume
                                                      \\
                             JobScout -> MatchRanker ---+

Reusable *steps* (``_step_*``) hold the real logic so the single-purpose runs
and ``run_full_pipeline`` share one implementation rather than two that drift.

Threading model: the DB layer is synchronous SQLAlchemy on SQLite. Every
database touch goes through :func:`db_call`, which runs a short unit of work on
a fresh ``Session`` in Starlette's threadpool. Helper functions therefore return
plain dicts/scalars, never live ORM instances, because the session is closed by
the time the caller sees the value.

Hermes never submits an application: ``run_job_tailor`` produces a tailored
resume and the apply URL, and a human clicks Apply.
"""

from __future__ import annotations

import asyncio
import logging
import os
from dataclasses import asdict, is_dataclass
from typing import Any, Awaitable, Callable, Iterable, Sequence, TypeVar

from sqlalchemy import func, select
from sqlalchemy.orm import Session
from starlette.concurrency import run_in_threadpool

from hermes.agents.ats import score_resume
from hermes.agents.job_scout import JobScout
from hermes.agents.match_ranker import MatchRanker
from hermes.agents.profile_analyst import ProfileAnalyst
from hermes.agents.resume_architect import ResumeArchitect
from hermes.db import SessionLocal
from hermes.llm import get_llm
from hermes.mcp_client import get_mcp
from hermes.models import Job, Profile, Resume, Run
from hermes.render import render_resume
from hermes.routes._common import (
    emit,
    get_setting,
    jdump,
    jload,
    job_payload,
    pk_value,
    safe_basename,
    utcnow,
)
from hermes.sandbox import get_sandbox
from hermes.settings import settings

log = logging.getLogger("hermes.pipeline")

T = TypeVar("T")

__all__ = [
    "run_profile_import",
    "run_job_search",
    "run_resume_build",
    "run_job_tailor",
    "run_ats_score",
    "run_sandbox_exec",
    "run_full_pipeline",
    "PROFILE_SECTIONS",
]

#: Sections requested from the LinkedIn MCP profile tools. The MCP server takes
#: this as a comma-ish string (verified against the tool signature in the
#: contract) and each extra section costs scrolls, so keep it to what the
#: ProfileAnalyst actually consumes.
PROFILE_SECTIONS = "experience,education,skills,certifications,projects"

#: Hard ceiling on LLM ranking calls per job_search run, so a 10-page search on
#: a free provider tier cannot turn into 500 requests.
MAX_RANKED_PER_RUN = 120

#: Concurrent MatchRanker calls. Free LLM tiers rate-limit aggressively, so the
#: default is deliberately low; override with the `rank_concurrency` setting.
DEFAULT_RANK_CONCURRENCY = 3

#: Where uploaded/generated resume text and artefacts live (see Dockerfile).
_RESUME_DIR = os.path.join(str(getattr(settings, "hermes_data_dir", "/data")), "resumes")


# --------------------------------------------------------------------------- #
# Database plumbing
# --------------------------------------------------------------------------- #


async def db_call(fn: Callable[..., T], *args: Any, **kwargs: Any) -> T:
    """Run one synchronous unit of DB work on a fresh Session, in a thread.

    Commits on success, rolls back on failure. ``fn`` receives the Session as
    its first argument and must return detached data (dicts/scalars), never a
    live ORM object.
    """

    def _work() -> T:
        with SessionLocal() as db:
            try:
                result = fn(db, *args, **kwargs)
                db.commit()
                return result
            except Exception:
                db.rollback()
                raise

    return await run_in_threadpool(_work)


def _run_pk(run_id: Any) -> Any:
    return pk_value(Run, run_id)


async def _set_run(run_id: str, **fields: Any) -> None:
    """Patch columns on a Run row (no-op if the row vanished)."""

    def _work(db: Session) -> None:
        run = db.get(Run, _run_pk(run_id))
        if run is None:
            log.warning("run %s disappeared while updating %s", run_id, list(fields))
            return
        for key, value in fields.items():
            setattr(run, key, value)

    await db_call(_work)


class _RunScope:
    """Mutable carrier for a pipeline's result payload."""

    __slots__ = ("result",)

    def __init__(self) -> None:
        self.result: dict[str, Any] = {}


async def _run_scope(run_id: str, kind: str, body: Callable[[_RunScope], Awaitable[None]]) -> dict[str, Any]:
    """Execute ``body`` as the lifecycle of one Run row.

    Guarantees exactly one terminal state is written. Returns the result payload
    (handy for tests and for ``POST /sandbox/exec`` which awaits inline).
    """
    scope = _RunScope()
    await _set_run(run_id, status="running", started_at=utcnow(), error=None)
    await emit(run_id, "info", f"{kind}: started")
    try:
        await body(scope)
    except asyncio.CancelledError:
        await emit(run_id, "error", f"{kind}: cancelled")
        await _set_run(
            run_id,
            status="error",
            error="Run cancelled (service shutting down or task cancelled).",
            finished_at=utcnow(),
        )
        raise
    except Exception as exc:
        detail = f"{type(exc).__name__}: {exc}"
        log.exception("run %s (%s) failed", run_id, kind)
        await emit(run_id, "error", f"{kind}: FAILED — {detail}")
        # Keep whatever partial result the body managed to record; it is often
        # the only clue about where a multi-step pipeline died.
        await _set_run(
            run_id,
            status="error",
            error=detail[:4000],
            result_json=jdump(scope.result) if scope.result else None,
            finished_at=utcnow(),
        )
        return {"error": detail}

    await _set_run(
        run_id,
        status="done",
        error=None,
        result_json=jdump(scope.result),
        finished_at=utcnow(),
    )
    await emit(run_id, "info", f"{kind}: done")
    return scope.result


# --------------------------------------------------------------------------- #
# Service handles
# --------------------------------------------------------------------------- #


def _llm_overrides(db: Session) -> dict[str, Any]:
    """Dashboard-selected model overrides from the Setting table."""
    primary = (get_setting(db, "model_primary", "") or "").strip()
    fallbacks = (get_setting(db, "model_fallbacks", "") or "").strip()
    return {
        "primary": primary or None,
        "fallbacks": [f.strip() for f in fallbacks.split(",") if f.strip()] or None,
    }


async def _llm():
    """LLM router with the dashboard's model choice applied, if any.

    ``get_llm()`` builds the router from environment config; the Settings page
    can override the model without a container restart, so we patch the
    instance attributes named in the ``LLMRouter.__init__`` contract.
    """
    llm = get_llm()
    try:
        overrides = await db_call(_llm_overrides)
    except Exception as exc:  # pragma: no cover - settings table unavailable
        log.warning("could not read model overrides: %s", exc)
        return llm
    if overrides["primary"]:
        setattr(llm, "primary", overrides["primary"])
    if overrides["fallbacks"]:
        setattr(llm, "fallbacks", overrides["fallbacks"])
    return llm


async def _rank_concurrency() -> int:
    try:
        raw = await db_call(get_setting, "rank_concurrency", str(DEFAULT_RANK_CONCURRENCY))
    except Exception:  # pragma: no cover
        return DEFAULT_RANK_CONCURRENCY
    try:
        return max(1, min(8, int(str(raw).strip())))
    except (TypeError, ValueError):
        return DEFAULT_RANK_CONCURRENCY


# --------------------------------------------------------------------------- #
# Raw-profile normalisation
#
# The MCP scraper's payload shape is not contractually fixed (and changes with
# LinkedIn's DOM), so every lookup is tolerant of several spellings and of the
# whole profile being nested one level down.
# --------------------------------------------------------------------------- #


def _unwrap_profile(raw: Any) -> dict[str, Any]:
    """Return the dict that actually holds profile fields."""
    if not isinstance(raw, dict):
        return {}
    for key in ("profile", "data", "result", "person"):
        inner = raw.get(key)
        if isinstance(inner, dict) and len(inner) >= len(raw) - 1:
            return inner
    return raw


def _pick(data: dict[str, Any], *names: str, default: Any = None) -> Any:
    """First non-empty value among ``names`` (case/separator insensitive)."""
    if not isinstance(data, dict):
        return default
    normalised = {"".join(ch for ch in str(k).lower() if ch.isalnum()): v for k, v in data.items()}
    for name in names:
        probe = "".join(ch for ch in name.lower() if ch.isalnum())
        value = normalised.get(probe)
        if value not in (None, "", [], {}):
            return value
    return default


def _as_list(value: Any) -> list[Any]:
    if value is None:
        return []
    if isinstance(value, list):
        return value
    if isinstance(value, tuple):
        return list(value)
    if isinstance(value, dict):
        # Some scrapes return {"0": {...}, "1": {...}} or {"items": [...]}.
        for key in ("items", "elements", "list", "values"):
            inner = value.get(key)
            if isinstance(inner, list):
                return inner
        return [value]
    return [value]


def _extract_facets(raw: Any, analysis: dict[str, Any]) -> dict[str, Any]:
    """Pull the denormalised Profile columns out of the raw scrape.

    Falls back to the ProfileAnalyst's structured output when the scrape does
    not expose a field, which is why this runs *after* analysis.
    """
    body = _unwrap_profile(raw)
    skills = _as_list(_pick(body, "skills", "skill", "top_skills", default=[]))
    if not skills:
        skills = list(analysis.get("hard_skills") or []) + list(analysis.get("tools") or [])
    return {
        "headline": str(_pick(body, "headline", "title", default="") or analysis.get("headline") or "")[:512],
        "summary": str(_pick(body, "summary", "about", "bio", default="") or analysis.get("summary") or ""),
        "skills": skills,
        "experience": _as_list(
            _pick(body, "experience", "experiences", "positions", "work_experience", default=[])
        ),
        "education": _as_list(_pick(body, "education", "educations", "schools", default=[])),
    }


# --------------------------------------------------------------------------- #
# Step: profile import + analysis
# --------------------------------------------------------------------------- #


async def _step_import_profile(run_id: str, linkedin_username: str | None) -> dict[str, Any]:
    """Scrape a profile via MCP, analyse it, persist a Profile row.

    Returns ``{"profile_id", "analysis", "raw", "headline"}``.
    """
    mcp = get_mcp()
    if linkedin_username:
        tool, args = "get_person_profile", {
            "linkedin_username": str(linkedin_username).strip(),
            "sections": PROFILE_SECTIONS,
        }
        await emit(run_id, "info", f"Fetching LinkedIn profile '{linkedin_username}' via MCP…")
    else:
        tool, args = "get_my_profile", {"sections": PROFILE_SECTIONS}
        await emit(run_id, "info", "Fetching your own LinkedIn profile via MCP…")

    # The generic `call` is the contractually stable entry point on LinkedInMCP;
    # the typed helpers are sugar over it.
    raw = await mcp.call(tool, args)
    if not isinstance(raw, dict) or not raw:
        raise RuntimeError(
            f"MCP tool {tool} returned no usable profile data. "
            "Is the LinkedIn session authenticated? Open the LinkedIn page in the "
            "dashboard and run the login viewer."
        )
    if "text" in raw and len(raw) == 1:
        # LinkedInMCP could not parse the tool output as JSON — surface it
        # instead of pretending we have a profile.
        snippet = str(raw["text"])[:400]
        raise RuntimeError(f"MCP tool {tool} returned unstructured text: {snippet}")

    await emit(run_id, "info", "Profile scraped; running ProfileAnalyst…")
    llm = await _llm()
    analyst = ProfileAnalyst(llm, mcp=mcp, run_id=run_id)
    analysis = await analyst.analyze(raw)
    if not isinstance(analysis, dict):
        raise RuntimeError("ProfileAnalyst returned a non-object analysis.")

    facets = _extract_facets(raw, analysis)

    def _save(db: Session) -> Any:
        profile = Profile(
            source="linkedin_mcp",
            linkedin_username=(str(linkedin_username).strip() if linkedin_username else None),
            headline=facets["headline"] or None,
            summary=facets["summary"] or None,
            raw_json=jdump(raw),
            skills_json=jdump(facets["skills"]),
            experience_json=jdump(facets["experience"]),
            education_json=jdump(facets["education"]),
            analysis_json=jdump(analysis),
            fetched_at=utcnow(),
        )
        db.add(profile)
        db.flush()
        return profile.id

    profile_id = await db_call(_save)
    await emit(
        run_id,
        "info",
        f"Profile #{profile_id} stored — {len(facets['experience'])} roles, "
        f"{len(facets['skills'])} skills, {len(analysis.get('keyword_bank') or [])} keywords.",
    )
    return {
        "profile_id": profile_id,
        "analysis": analysis,
        "raw": raw,
        "headline": facets["headline"],
    }


def _load_profile(db: Session, profile_id: Any | None = None) -> dict[str, Any] | None:
    """Load a Profile (by id, else the most recent) as a detached dict."""
    profile = None
    if profile_id is not None:
        try:
            profile = db.get(Profile, pk_value(Profile, profile_id))
        except (TypeError, ValueError):
            profile = None
    if profile is None:
        profile = db.execute(
            select(Profile).order_by(Profile.fetched_at.desc(), Profile.id.desc()).limit(1)
        ).scalars().first()
    if profile is None:
        return None
    return {
        "id": profile.id,
        "headline": profile.headline,
        "summary": profile.summary,
        "raw": jload(profile.raw_json, {}) or {},
        "analysis": jload(profile.analysis_json, {}) or {},
        "skills": jload(profile.skills_json, []) or [],
    }


# --------------------------------------------------------------------------- #
# Step: resume build (architect -> render -> ATS score)
# --------------------------------------------------------------------------- #


def _next_version(db: Session, profile_id: Any) -> int:
    current = db.execute(
        select(func.max(Resume.version)).where(Resume.profile_id == profile_id)
    ).scalar()
    return int(current or 0) + 1


def _load_job(db: Session, job_id: Any) -> dict[str, Any] | None:
    try:
        job = db.get(Job, pk_value(Job, job_id))
    except (TypeError, ValueError):
        return None
    if job is None:
        return None
    payload = job_payload(job)
    payload["match_breakdown"] = jload(job.match_breakdown_json, {}) or {}
    return payload


async def _step_build_resume(
    run_id: str,
    *,
    profile_id: Any | None = None,
    target_job_id: Any | None = None,
    label: str | None = None,
) -> dict[str, Any]:
    """Build → render → score one resume version and persist it.

    Returns ``{"resume_id","profile_id","version","label","ats","paths","markdown_chars"}``.
    """
    profile = await db_call(_load_profile, profile_id)
    if profile is None:
        raise RuntimeError(
            "No LinkedIn profile stored yet. Run POST /api/profile/import first "
            "(LinkedIn page → Import profile)."
        )
    analysis = profile["analysis"]
    if not analysis:
        raise RuntimeError(
            f"Profile #{profile['id']} has no analysis payload; re-run the profile import."
        )

    target_job: dict[str, Any] | None = None
    if target_job_id is not None:
        target_job = await db_call(_load_job, target_job_id)
        if target_job is None:
            raise RuntimeError(f"Target job {target_job_id!r} not found.")
        await emit(
            run_id,
            "info",
            f"Tailoring for '{target_job.get('title')}' @ {target_job.get('company')}",
        )

    base_resume_text = await db_call(get_setting, "uploaded_resume_text", None)
    if base_resume_text:
        await emit(run_id, "info", f"Using uploaded base resume ({len(base_resume_text)} chars) as source material.")

    llm = await _llm()
    architect = ResumeArchitect(llm, run_id=run_id)
    built = await architect.build(
        analysis,
        profile["raw"],
        target_job=target_job,
        base_resume_text=base_resume_text,
    )
    markdown = (built or {}).get("markdown") or ""
    if not markdown.strip():
        raise RuntimeError("ResumeArchitect produced an empty resume.")

    version = await db_call(_next_version, profile["id"])
    if not label:
        if target_job:
            label = f"Tailored v{version} — {target_job.get('company') or 'job'} / {target_job.get('title') or ''}".strip(" /—")
        else:
            label = f"Base resume v{version}"

    basename = safe_basename(f"hermes_resume_p{profile['id']}_v{version}", f"hermes_resume_v{version}")
    await emit(run_id, "info", "Rendering ATS-safe .docx / .txt / .pdf in the sandbox…")
    try:
        paths = await render_resume(markdown, basename, run_id=run_id) or {}
    except Exception as exc:
        # A rendering failure must not lose the generated markdown — the user can
        # still read/copy it from the Resume page — so degrade instead of dying.
        await emit(run_id, "warn", f"Rendering failed ({exc}); keeping markdown only.")
        log.warning("render_resume failed for run %s: %s", run_id, exc)
        paths = {}

    job_description = (target_job or {}).get("description") or None
    await emit(run_id, "info", "Scoring resume against the ATS heuristics…")
    ats = await score_resume(llm, markdown, job_description, run_id=run_id) or {}
    score = float(ats.get("score") or 0.0)

    md_path = _write_markdown_sidecar(basename, markdown)

    def _save(db: Session) -> Any:
        resume = Resume(
            profile_id=profile["id"],
            version=version,
            label=label,
            target_job_id=(pk_value(Job, target_job_id) if target_job_id is not None else None),
            markdown=markdown,
            docx_path=paths.get("docx"),
            pdf_path=paths.get("pdf"),
            txt_path=paths.get("txt") or md_path,
            ats_score=score,
            ats_breakdown_json=jdump(ats),
            created_at=utcnow(),
        )
        db.add(resume)
        db.flush()
        return resume.id

    resume_id = await db_call(_save)
    await emit(
        run_id,
        "info",
        f"Resume #{resume_id} ({label}) stored — ATS score {score:.1f}/100.",
    )
    return {
        "resume_id": resume_id,
        "profile_id": profile["id"],
        "version": version,
        "label": label,
        "target_job_id": target_job_id,
        "ats": ats,
        "paths": paths,
        "markdown_chars": len(markdown),
        "keywords_used": (built or {}).get("keywords_used") or [],
    }


def _write_markdown_sidecar(basename: str, markdown: str) -> str | None:
    """Persist the markdown next to the rendered artefacts.

    Lets ``GET /resumes/{id}/download?fmt=md`` serve a real FileResponse, and
    gives the user a readable copy on the volume even if .docx rendering broke.
    """
    try:
        os.makedirs(_RESUME_DIR, exist_ok=True)
        path = os.path.join(_RESUME_DIR, f"{basename}.md")
        with open(path, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(markdown)
        return path
    except OSError as exc:  # pragma: no cover - read-only volume
        log.warning("could not write markdown sidecar %s: %s", basename, exc)
        return None


# --------------------------------------------------------------------------- #
# Step: job search + ranking
# --------------------------------------------------------------------------- #

#: Keys forwarded verbatim to JobScout.search (contract signature).
_SEARCH_KEYS = (
    "keywords",
    "location",
    "easy_apply",
    "max_pages",
    "date_posted",
    "job_type",
    "experience_level",
    "work_type",
    "sort_by",
    "fetch_details",
    "limit_details",
)


def _upsert_job(db: Session, item: dict[str, Any]) -> tuple[Any, bool]:
    """Insert-or-update a scraped job keyed on ``linkedin_job_id``.

    Returns ``(job_id, created)``. Never duplicates, and never clobbers a good
    stored description with an empty one from a shallower re-scrape.
    """
    linkedin_job_id = str(item.get("linkedin_job_id") or "").strip()
    if not linkedin_job_id:
        raise ValueError("scraped job has no linkedin_job_id")

    job = db.execute(
        select(Job).where(Job.linkedin_job_id == linkedin_job_id)
    ).scalars().first()
    created = job is None
    if job is None:
        job = Job(
            linkedin_job_id=linkedin_job_id,
            status="new",
            discovered_at=utcnow(),
        )
        db.add(job)

    job.title = item.get("title") or job.title
    job.company = item.get("company") or job.company
    job.location = item.get("location") or job.location
    job.url = item.get("url") or job.url
    job.posted = item.get("posted") or job.posted
    if item.get("easy_apply") is not None:
        job.easy_apply = bool(item.get("easy_apply"))
    description = item.get("description")
    if description:
        job.description = description
    raw = item.get("raw")
    if raw:
        job.raw_json = jdump(raw)
    db.flush()
    return job.id, created


def _store_rank(db: Session, job_id: Any, ranking: dict[str, Any]) -> None:
    job = db.get(Job, pk_value(Job, job_id))
    if job is None:
        return
    job.match_score = float(ranking.get("score") or 0.0)
    job.match_breakdown_json = jdump(ranking)


def _jobs_needing_rank(db: Session, job_ids: Sequence[Any], rerank_all: bool) -> list[dict[str, Any]]:
    """Payloads for the jobs that still need a MatchRanker pass."""
    out: list[dict[str, Any]] = []
    for job_id in job_ids:
        job = db.get(Job, pk_value(Job, job_id))
        if job is None:
            continue
        if not rerank_all and job.match_score is not None:
            continue
        out.append(job_payload(job))
    return out


async def _step_search_jobs(
    run_id: str,
    params: dict[str, Any],
    analysis: dict[str, Any] | None,
) -> dict[str, Any]:
    """Scout jobs via MCP, upsert them, then rank the unranked ones."""
    keywords = str(params.get("keywords") or "").strip()
    if not keywords:
        keywords = str(await db_call(get_setting, "default_job_keywords", "") or "").strip()
    if not keywords and analysis:
        # Fall back to the analyst's target roles so a one-click search works
        # straight after a profile import.
        roles = [str(r) for r in (analysis.get("target_roles") or []) if str(r).strip()]
        keywords = " OR ".join(roles[:3])
    if not keywords:
        raise ValueError(
            "No search keywords. Provide `keywords`, set the `default_job_keywords` "
            "setting, or import a profile first so target roles can be inferred."
        )

    location = params.get("location")
    if location in (None, ""):
        location = await db_call(get_setting, "default_job_location", None)

    kwargs: dict[str, Any] = {"keywords": keywords}
    if location:
        kwargs["location"] = str(location)
    for key in _SEARCH_KEYS:
        if key in ("keywords", "location"):
            continue
        value = params.get(key)
        if value is not None:
            kwargs[key] = value
    # LinkedIn rejects max_pages outside 1..10 (verified tool constraint).
    if "max_pages" in kwargs:
        try:
            kwargs["max_pages"] = max(1, min(10, int(kwargs["max_pages"])))
        except (TypeError, ValueError):
            kwargs.pop("max_pages")

    await emit(run_id, "info", f"Searching LinkedIn jobs for '{keywords}'" + (f" in {location}" if location else "") + "…")
    llm = await _llm()
    scout = JobScout(llm, mcp=get_mcp(), run_id=run_id)
    found = await scout.search(**kwargs)
    items = [item for item in (found or []) if isinstance(item, dict)]
    await emit(run_id, "info", f"Scout returned {len(items)} job postings.")

    created_ids: list[Any] = []
    all_ids: list[Any] = []
    skipped = 0
    for item in items:
        try:
            job_id, created = await db_call(_upsert_job, item)
        except Exception as exc:
            skipped += 1
            log.warning("skipping unusable job payload: %s", exc)
            continue
        all_ids.append(job_id)
        if created:
            created_ids.append(job_id)
    if skipped:
        await emit(run_id, "warn", f"{skipped} postings skipped (missing job id).")
    await emit(run_id, "info", f"{len(created_ids)} new, {len(all_ids) - len(created_ids)} updated.")

    ranked = 0
    if not analysis:
        await emit(
            run_id,
            "warn",
            "No profile analysis available — jobs stored unranked. Import your profile to enable scoring.",
        )
    elif all_ids:
        rerank_all = bool(params.get("rerank_all"))
        todo = await db_call(_jobs_needing_rank, all_ids, rerank_all)
        todo = todo[:MAX_RANKED_PER_RUN]
        if todo:
            ranked = await _rank_jobs(run_id, todo, analysis)

    return {
        "keywords": keywords,
        "location": location,
        "found": len(items),
        "new": len(created_ids),
        "updated": len(all_ids) - len(created_ids),
        "ranked": ranked,
        "job_ids": [str(j) for j in all_ids],
        "new_job_ids": [str(j) for j in created_ids],
    }


async def _rank_jobs(run_id: str, jobs: list[dict[str, Any]], analysis: dict[str, Any]) -> int:
    """Score jobs with MatchRanker under bounded concurrency. Returns count scored."""
    llm = await _llm()
    ranker = MatchRanker(llm, run_id=run_id)
    limit = await _rank_concurrency()
    semaphore = asyncio.Semaphore(limit)
    done = 0
    total = len(jobs)
    await emit(run_id, "info", f"Ranking {total} jobs against your profile (concurrency {limit})…")

    async def _one(payload: dict[str, Any]) -> bool:
        nonlocal done
        async with semaphore:
            try:
                ranking = await ranker.rank(payload, analysis)
            except Exception as exc:
                # One bad job must not fail the whole search.
                log.warning("ranking failed for job %s: %s", payload.get("linkedin_job_id"), exc)
                await emit(run_id, "warn", f"Could not rank '{payload.get('title')}': {exc}")
                return False
            if not isinstance(ranking, dict):
                return False
            await db_call(_store_rank, payload["id"], ranking)
            done += 1
            if done % 10 == 0 or done == total:
                await emit(run_id, "info", f"Ranked {done}/{total} jobs.")
            return True

    await asyncio.gather(*(_one(payload) for payload in jobs), return_exceptions=True)
    return done


# --------------------------------------------------------------------------- #
# Public pipelines
# --------------------------------------------------------------------------- #


async def run_profile_import(run_id: str, linkedin_username: str | None = None) -> dict[str, Any]:
    """Scrape + analyse a LinkedIn profile and store it."""

    async def _body(scope: _RunScope) -> None:
        out = await _step_import_profile(run_id, linkedin_username)
        scope.result = {
            "profile_id": out["profile_id"],
            "headline": out["headline"],
            "analysis": out["analysis"],
        }

    return await _run_scope(run_id, "profile_import", _body)


async def run_job_search(run_id: str, params: dict[str, Any]) -> dict[str, Any]:
    """Search LinkedIn, upsert Job rows on ``linkedin_job_id``, rank new jobs."""

    async def _body(scope: _RunScope) -> None:
        profile = await db_call(_load_profile, params.get("profile_id"))
        analysis = (profile or {}).get("analysis") or None
        scope.result = await _step_search_jobs(run_id, params or {}, analysis)

    return await _run_scope(run_id, "job_search", _body)


async def run_resume_build(
    run_id: str,
    profile_id: Any | None = None,
    target_job_id: Any | None = None,
) -> dict[str, Any]:
    """Generate one resume version (optionally aimed at a specific job)."""

    async def _body(scope: _RunScope) -> None:
        scope.result = await _step_build_resume(
            run_id, profile_id=profile_id, target_job_id=target_job_id
        )

    return await _run_scope(run_id, "resume_build", _body)


async def run_job_tailor(run_id: str, job_id: Any) -> dict[str, Any]:
    """Build a job-specific resume, score it against that JD, and link it.

    Sets ``Job.tailored_resume_id`` and moves the job to ``status="tailored"``.
    No application is submitted — the dashboard hands the user the apply URL.
    """

    async def _body(scope: _RunScope) -> None:
        job = await db_call(_load_job, job_id)
        if job is None:
            raise RuntimeError(f"Job {job_id!r} not found.")
        if not (job.get("description") or "").strip():
            await emit(
                run_id,
                "warn",
                "This job has no scraped description; tailoring will rely on title/company only. "
                "Re-run the search with fetch_details enabled for a better result.",
            )

        built = await _step_build_resume(run_id, target_job_id=job_id)

        def _link(db: Session) -> None:
            row = db.get(Job, pk_value(Job, job_id))
            if row is None:
                return
            row.tailored_resume_id = built["resume_id"]
            # Never regress a job the user already moved past tailoring.
            if row.status in (None, "", "new", "shortlisted"):
                row.status = "tailored"

        await db_call(_link)
        scope.result = {
            **built,
            "job_id": job.get("id"),
            "job_title": job.get("title"),
            "job_company": job.get("company"),
            "apply_url": job.get("url"),
            "note": "Hermes does not submit applications — open the apply URL and submit manually.",
        }

    return await _run_scope(run_id, "job_tailor", _body)


async def run_ats_score(run_id: str, resume_id: Any, job_id: Any | None = None) -> dict[str, Any]:
    """Re-score an existing resume (optionally against a specific job's JD)."""

    async def _body(scope: _RunScope) -> None:
        def _load(db: Session) -> dict[str, Any] | None:
            resume = db.get(Resume, pk_value(Resume, resume_id))
            if resume is None:
                return None
            return {"id": resume.id, "markdown": resume.markdown or "", "label": resume.label}

        resume = await db_call(_load)
        if resume is None:
            raise RuntimeError(f"Resume {resume_id!r} not found.")
        if not resume["markdown"].strip():
            raise RuntimeError(f"Resume #{resume['id']} has no markdown to score.")

        job_description = None
        job_meta: dict[str, Any] = {}
        if job_id is not None:
            job = await db_call(_load_job, job_id)
            if job is None:
                raise RuntimeError(f"Job {job_id!r} not found.")
            job_description = job.get("description") or None
            job_meta = {"job_id": job.get("id"), "job_title": job.get("title")}
            if not job_description:
                await emit(run_id, "warn", "Job has no description — scoring against the profile keyword bank instead.")

        llm = await _llm()
        ats = await score_resume(llm, resume["markdown"], job_description, run_id=run_id) or {}

        def _save(db: Session) -> None:
            row = db.get(Resume, pk_value(Resume, resume_id))
            if row is None:
                return
            row.ats_score = float(ats.get("score") or 0.0)
            row.ats_breakdown_json = jdump(ats)

        await db_call(_save)
        await emit(run_id, "info", f"ATS score: {float(ats.get('score') or 0.0):.1f}/100")
        scope.result = {"resume_id": resume["id"], "ats": ats, **job_meta}

    return await _run_scope(run_id, "ats_score", _body)


async def run_sandbox_exec(
    run_id: str,
    code: str,
    files: dict[str, str] | None = None,
    timeout: int | None = None,
    network: str | None = None,
) -> dict[str, Any]:
    """Execute Python in an ephemeral hardened sandbox container."""

    async def _body(scope: _RunScope) -> None:
        if not (code or "").strip():
            raise ValueError("`code` is required and must not be empty.")
        sandbox = get_sandbox()
        await emit(run_id, "info", "Spawning hermes-sandbox container…")
        result = await sandbox.run_python(
            code,
            files=files or None,
            timeout=timeout,
            run_id=run_id,
            network=network,
        )
        payload = asdict(result) if is_dataclass(result) else dict(result)  # type: ignore[arg-type]
        await emit(run_id, "info", f"Sandbox exited with code {payload.get('exit_code')}.")
        scope.result = payload

    return await _run_scope(run_id, "sandbox_exec", _body)


async def run_full_pipeline(run_id: str, params: dict[str, Any]) -> dict[str, Any]:
    """import -> analyze -> resume -> search -> rank, as one Run.

    Every stage is recorded into ``result_json`` as it completes, so a failure
    halfway through still tells the dashboard exactly how far Hermes got.
    Optional: ``tailor_top_n`` tailors the highest-scoring jobs afterwards.
    """
    params = params or {}

    async def _body(scope: _RunScope) -> None:
        stages: dict[str, Any] = {}
        scope.result = {"stages": stages}

        # 1 + 2 — import and analyse.
        imported = await _step_import_profile(run_id, params.get("linkedin_username"))
        profile_id = imported["profile_id"]
        analysis = imported["analysis"]
        stages["profile_import"] = {
            "profile_id": profile_id,
            "headline": imported["headline"],
            "target_roles": analysis.get("target_roles") or [],
        }

        # 3 — base resume.
        if params.get("build_resume", True):
            built = await _step_build_resume(run_id, profile_id=profile_id)
            stages["resume_build"] = {
                "resume_id": built["resume_id"],
                "version": built["version"],
                "ats_score": (built.get("ats") or {}).get("score"),
            }
        else:
            await emit(run_id, "info", "Skipping resume build (build_resume=false).")

        # 4 + 5 — search and rank.
        search_params = {k: v for k, v in params.items() if k in _SEARCH_KEYS or k == "rerank_all"}
        stages["job_search"] = await _step_search_jobs(run_id, search_params, analysis)

        # Optional 6 — tailor the best matches.
        top_n = _int_or_zero(params.get("tailor_top_n"))
        if top_n > 0:
            tailored = await _tailor_top_jobs(run_id, top_n, _float_or(params.get("min_score"), 0.0))
            stages["tailored"] = tailored

        await emit(
            run_id,
            "info",
            f"Full pipeline complete — {stages['job_search']['found']} jobs seen, "
            f"{stages['job_search']['ranked']} ranked. Open the Jobs page to apply.",
        )

    return await _run_scope(run_id, "full_pipeline", _body)


def _int_or_zero(value: Any) -> int:
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        return 0


def _float_or(value: Any, default: float) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


async def _tailor_top_jobs(run_id: str, top_n: int, min_score: float) -> list[dict[str, Any]]:
    """Tailor resumes for the best-scoring untailored jobs (sequential on purpose)."""

    def _pick(db: Session) -> list[Any]:
        rows = db.execute(
            select(Job.id)
            .where(Job.tailored_resume_id.is_(None))
            .where(Job.match_score.isnot(None))
            .where(Job.match_score >= min_score)
            .order_by(Job.match_score.desc())
            .limit(top_n)
        ).scalars().all()
        return list(rows)

    job_ids = await db_call(_pick)
    out: list[dict[str, Any]] = []
    for job_id in job_ids:
        job = await db_call(_load_job, job_id)
        if job is None:
            continue
        await emit(run_id, "info", f"Tailoring resume for '{job.get('title')}' @ {job.get('company')}…")
        try:
            built = await _step_build_resume(run_id, target_job_id=job_id)
        except Exception as exc:
            await emit(run_id, "warn", f"Tailoring failed for job {job_id}: {exc}")
            continue

        def _link(db: Session, _job_id: Any = job_id, _resume_id: Any = built["resume_id"]) -> None:
            row = db.get(Job, pk_value(Job, _job_id))
            if row is None:
                return
            row.tailored_resume_id = _resume_id
            if row.status in (None, "", "new", "shortlisted"):
                row.status = "tailored"

        await db_call(_link)
        out.append(
            {
                "job_id": job.get("id"),
                "title": job.get("title"),
                "company": job.get("company"),
                "resume_id": built["resume_id"],
                "ats_score": (built.get("ats") or {}).get("score"),
                "apply_url": job.get("url"),
            }
        )
    return out
