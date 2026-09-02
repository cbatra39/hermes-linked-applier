"""Shared helpers for the Hermes HTTP layer (and the pipeline that feeds it).

Everything in here is deliberately dependency-light and defensive:

* **Serialisers** turn SQLAlchemy rows into plain JSON-able dicts. They use
  ``getattr(obj, name, None)`` so a column added later by ``models.py`` cannot
  break a response, and they never leak a huge blob (``raw_json``, resume
  markdown) into a *list* response.
* **Primary-key coercion** — the contract does not pin ``Run.id`` / ``Job.id``
  to ``int`` or ``str``, so we introspect the mapped column type once and cast
  path parameters accordingly. This keeps the routes correct whether
  ``models.py`` uses autoincrement integers or UUID strings.
* **SSE** — one battle-tested async generator used by both
  ``GET /runs/{id}/events`` and ``GET /containers/{id}/logs``.
* **Settings rows** — tiny get/set over the ``Setting`` key/value table.

``hermes.pipeline`` imports the JSON / Setting / payload helpers from here so
there is exactly one implementation of each. That makes this module the shared
utility belt of the API surface rather than a strictly route-only module.
"""

from __future__ import annotations

import asyncio
import inspect
import json
import logging
import re
from datetime import date, datetime, timezone
from typing import Any, AsyncIterator, Awaitable, Callable, Iterable, Mapping, Sequence

from fastapi import HTTPException, Request
from sqlalchemy import select
from sqlalchemy.orm import Session

from hermes import __version__ as HERMES_VERSION
from hermes.events import bus
from hermes.models import Setting

log = logging.getLogger("hermes.api")

__all__ = [
    "HERMES_VERSION",
    "SSE_HEADERS",
    "JOB_STATUSES",
    "RUN_KINDS",
    "RUN_STATUSES",
    "utcnow",
    "iso",
    "jload",
    "jdump",
    "pk_python_type",
    "pk_value",
    "coerce_pk",
    "get_setting",
    "set_setting",
    "all_settings",
    "emit",
    "sse_pack",
    "sse_comment",
    "sse_stream",
    "subscribe_iter",
    "safe_basename",
    "run_dict",
    "event_dict",
    "job_dict",
    "job_payload",
    "resume_dict",
    "profile_dict",
]

# --------------------------------------------------------------------------- #
# Constants
# --------------------------------------------------------------------------- #

#: Required by the contract for every text/event-stream response.
#: ``X-Accel-Buffering: no`` stops the dashboard's nginx from buffering the
#: stream (without it the log drawer appears frozen for ~4 KB at a time).
SSE_HEADERS: dict[str, str] = {
    "Cache-Control": "no-cache, no-transform",
    "Connection": "keep-alive",
    "X-Accel-Buffering": "no",
}

#: Job.status domain, per contract.
JOB_STATUSES: tuple[str, ...] = (
    "new",
    "shortlisted",
    "tailored",
    "applied",
    "rejected",
    "skipped",
)

#: Run.kind domain, per contract.
RUN_KINDS: tuple[str, ...] = (
    "profile_import",
    "resume_build",
    "job_search",
    "job_tailor",
    "ats_score",
    "sandbox_exec",
    "full_pipeline",
)

#: Run.status domain, per contract.
RUN_STATUSES: tuple[str, ...] = ("pending", "running", "done", "error")

_TERMINAL_RUN_STATUSES = frozenset({"done", "error"})

_SAFE_NAME_RE = re.compile(r"[^A-Za-z0-9._-]+")
_SETTING_KEY_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.\-]{0,63}$")


# --------------------------------------------------------------------------- #
# Time / JSON
# --------------------------------------------------------------------------- #


def utcnow() -> datetime:
    """Timezone-aware UTC now (naive datetimes cause silent TZ bugs in SQLite)."""
    return datetime.now(timezone.utc)


def iso(value: Any) -> str | None:
    """Serialise a datetime/date to ISO-8601; pass through None; str() anything else."""
    if value is None:
        return None
    if isinstance(value, datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value.isoformat()
    if isinstance(value, date):
        return value.isoformat()
    return str(value)


def jload(raw: Any, default: Any = None) -> Any:
    """Parse a ``*_json`` Text column. Never raises — bad JSON yields ``default``.

    Also tolerates a column that already holds a parsed object (which happens if
    ``models.py`` exposes a helper property with the same name).
    """
    if raw is None or raw == "":
        return default
    if isinstance(raw, (dict, list, int, float, bool)):
        return raw
    try:
        return json.loads(raw)
    except (TypeError, ValueError):
        log.debug("jload: column did not contain valid JSON (%.60r)", raw)
        return default


def jdump(value: Any) -> str:
    """Serialise to a compact JSON string for a ``*_json`` Text column.

    ``default=str`` keeps datetimes/Decimals from exploding a whole pipeline run
    just because a scraper handed us an exotic type.
    """
    return json.dumps(value, ensure_ascii=False, default=str)


def safe_basename(name: str, fallback: str = "hermes") -> str:
    """Filesystem-safe basename (Windows + Linux): no separators, no spaces."""
    cleaned = _SAFE_NAME_RE.sub("_", str(name or "")).strip("._-")
    return cleaned[:120] or fallback


# --------------------------------------------------------------------------- #
# Primary keys — the contract leaves the id column type open, so introspect it
# --------------------------------------------------------------------------- #


def pk_python_type(model: type) -> type:
    """Python type of ``model.id`` (``int`` or ``str``), defaulting to ``str``."""
    try:
        column = model.__table__.c["id"]
        return column.type.python_type  # type: ignore[no-any-return]
    except Exception:  # pragma: no cover - exotic/custom column types
        return str


def pk_value(model: type, value: Any) -> Any:
    """Cast ``value`` to ``model.id``'s python type. Raises ValueError if impossible."""
    if pk_python_type(model) is int:
        return int(value)
    return str(value)


def coerce_pk(model: type, value: Any) -> Any:
    """HTTP-facing :func:`pk_value` — a malformed id is a 400, not a 500."""
    try:
        return pk_value(model, value)
    except (TypeError, ValueError):
        raise HTTPException(
            status_code=400,
            detail=f"Invalid {model.__name__.lower()} id {value!r}: expected an integer.",
        ) from None


# --------------------------------------------------------------------------- #
# Setting key/value table
# --------------------------------------------------------------------------- #


def get_setting(db: Session, key: str, default: str | None = None) -> str | None:
    """Read one editable Setting row."""
    row = db.get(Setting, key)
    if row is None or row.value is None:
        return default
    return row.value


def set_setting(db: Session, key: str, value: Any) -> None:
    """Upsert one editable Setting row (values are always stored as text)."""
    text = "" if value is None else str(value)
    row = db.get(Setting, key)
    if row is None:
        db.add(Setting(key=key, value=text))
    else:
        row.value = text


def all_settings(db: Session) -> dict[str, str]:
    """Every Setting row as a plain dict."""
    rows = db.execute(select(Setting)).scalars().all()
    return {row.key: (row.value or "") for row in rows}


def validate_setting_key(key: str) -> str:
    """Reject keys that would be unusable/abusive as a dashboard setting name."""
    if not _SETTING_KEY_RE.match(str(key or "")):
        raise HTTPException(
            status_code=422,
            detail=(
                f"Invalid setting key {key!r}: use 1-64 chars of "
                "[A-Za-z0-9_.-] starting with a letter or digit."
            ),
        )
    return str(key)


# --------------------------------------------------------------------------- #
# Event bus adapters
#
# The contract specifies EventBus.publish/subscribe but not whether publish is a
# coroutine. Both shapes are supported here so the API layer cannot break when
# hermes/events.py picks one.
# --------------------------------------------------------------------------- #


async def emit(run_id: str | None, level: str, message: str) -> None:
    """Publish a run event. Telemetry failures are logged, never propagated."""
    if not run_id:
        return
    try:
        result = bus.publish(str(run_id), level, str(message))
        if inspect.isawaitable(result):
            await result
    except Exception as exc:  # pragma: no cover - best-effort telemetry
        log.warning("event publish failed for run %s: %s", run_id, exc)


async def subscribe_iter(run_id: str) -> AsyncIterator[str]:
    """``bus.subscribe`` normalised to an async iterator.

    Supports a plain async-generator function, a coroutine returning an
    iterator, and an object exposing ``__aiter__``.
    """
    source: Any = bus.subscribe(str(run_id))
    if inspect.isawaitable(source):
        source = await source
    if hasattr(source, "__aiter__"):
        return source.__aiter__()
    raise TypeError(f"bus.subscribe() returned a non-async-iterable: {type(source)!r}")


# --------------------------------------------------------------------------- #
# Server-Sent Events
# --------------------------------------------------------------------------- #


def sse_pack(chunk: Any) -> str:
    """Format one SSE ``data:`` frame.

    Delegates to ``bus.sse_format`` when available (so the wire format stays
    owned by events.py), falls back to a spec-correct encoder, and passes
    through payloads that are already framed.
    """
    if isinstance(chunk, str) and (chunk.startswith("data:") or chunk.startswith(":")):
        return chunk if chunk.endswith("\n\n") else chunk + "\n\n"

    formatter = getattr(bus, "sse_format", None)
    if callable(formatter):
        try:
            framed = formatter(chunk)
            if isinstance(framed, str) and framed:
                return framed if framed.endswith("\n\n") else framed + "\n\n"
        except Exception:  # pragma: no cover - fall through to local encoder
            log.debug("bus.sse_format failed; using local SSE encoder", exc_info=True)

    text = chunk if isinstance(chunk, str) else jdump(chunk)
    # A payload containing newlines must be split across multiple data: lines.
    body = "\n".join(f"data: {line}" for line in str(text).split("\n"))
    return body + "\n\n"


def sse_comment(text: str = "keepalive") -> str:
    """An SSE comment frame — ignored by EventSource, but keeps proxies awake."""
    return f": {text}\n\n"


async def sse_stream(
    aiter_factory: Callable[[], Awaitable[AsyncIterator[str]]],
    *,
    request: Request | None = None,
    replay: Sequence[Any] = (),
    keepalive: float = 15.0,
    stop_check: Callable[[], Awaitable[bool]] | None = None,
    final: Any = None,
) -> AsyncIterator[str]:
    """Generic SSE body generator.

    Parameters
    ----------
    aiter_factory:
        Awaitable returning the upstream async iterator of payloads.
    replay:
        Payloads flushed before subscribing (persisted history, log tail).
    keepalive:
        Seconds between ``: keepalive`` comments when the source is idle.
    stop_check:
        Awaited on every keepalive tick; returning True ends the stream (used to
        close the stream once a Run reaches a terminal state).
    final:
        Payload emitted just before a ``stop_check``-triggered close.

    Implementation note: instead of ``asyncio.wait_for`` on the upstream
    ``__anext__`` (which throws CancelledError *into* the upstream generator on
    every idle tick and can drop a queued item), a pump task drains the upstream
    into a local queue. Cancelling ``queue.get()`` is always safe.
    """
    sentinel = object()
    queue: asyncio.Queue[Any] = asyncio.Queue(maxsize=1000)
    pump: asyncio.Task[None] | None = None

    async def _pump() -> None:
        try:
            source = await aiter_factory()
            async for item in source:
                await queue.put(item)
        except asyncio.CancelledError:  # pragma: no cover - normal teardown
            raise
        except Exception as exc:
            log.warning("SSE upstream failed: %s", exc)
            await queue.put(sse_pack({"level": "error", "message": f"stream error: {exc}"}))
        finally:
            queue.put_nowait(sentinel)

    try:
        # Open the stream immediately: browsers only fire EventSource.onopen
        # once the first byte arrives, and nginx needs a byte to flush headers.
        yield sse_comment("stream open")
        for item in replay:
            yield sse_pack(item)

        pump = asyncio.create_task(_pump())

        while True:
            if request is not None and await request.is_disconnected():
                break
            try:
                item = await asyncio.wait_for(queue.get(), timeout=keepalive)
            except asyncio.TimeoutError:
                yield sse_comment()
                if stop_check is not None and await stop_check():
                    if final is not None:
                        yield sse_pack(final)
                    break
                continue
            if item is sentinel:
                if final is not None:
                    yield sse_pack(final)
                break
            yield sse_pack(item)
    except asyncio.CancelledError:  # pragma: no cover - client vanished
        raise
    finally:
        if pump is not None and not pump.done():
            pump.cancel()


# --------------------------------------------------------------------------- #
# Row serialisers
# --------------------------------------------------------------------------- #


def run_dict(run: Any, *, events: Iterable[Any] | None = None) -> dict[str, Any]:
    """Serialise a Run row. ``id`` is always a string so the UI can key on it."""
    out: dict[str, Any] = {
        "id": str(getattr(run, "id", "")),
        "kind": getattr(run, "kind", None),
        "status": getattr(run, "status", None),
        "params": jload(getattr(run, "params_json", None), {}),
        "result": jload(getattr(run, "result_json", None), None),
        "error": getattr(run, "error", None),
        "started_at": iso(getattr(run, "started_at", None)),
        "finished_at": iso(getattr(run, "finished_at", None)),
    }
    if events is not None:
        out["events"] = [event_dict(e) for e in events]
    return out


def event_dict(event: Any) -> dict[str, Any]:
    """Serialise a RunEvent row."""
    return {
        "id": getattr(event, "id", None),
        "run_id": str(getattr(event, "run_id", "")),
        "ts": iso(getattr(event, "ts", None)),
        "level": getattr(event, "level", "info"),
        "message": getattr(event, "message", ""),
    }


def job_dict(job: Any, *, full: bool = False) -> dict[str, Any]:
    """Serialise a Job row.

    List responses stay small: the (potentially 20 KB) description is replaced
    by a preview and ``raw_json`` is omitted entirely unless ``full=True``.
    """
    description = getattr(job, "description", None) or ""
    out: dict[str, Any] = {
        "id": getattr(job, "id", None),
        "linkedin_job_id": getattr(job, "linkedin_job_id", None),
        "title": getattr(job, "title", None),
        "company": getattr(job, "company", None),
        "location": getattr(job, "location", None),
        "url": getattr(job, "url", None),
        "easy_apply": bool(getattr(job, "easy_apply", False)),
        "posted": getattr(job, "posted", None),
        "discovered_at": iso(getattr(job, "discovered_at", None)),
        "match_score": getattr(job, "match_score", None),
        "status": getattr(job, "status", None),
        "applied_at": iso(getattr(job, "applied_at", None)),
        "notes": getattr(job, "notes", None),
        "tailored_resume_id": getattr(job, "tailored_resume_id", None),
        "has_description": bool(description),
    }
    if full:
        out["description"] = description
        out["match_breakdown"] = jload(getattr(job, "match_breakdown_json", None), {})
        out["raw"] = jload(getattr(job, "raw_json", None), {})
    else:
        out["description_preview"] = description[:280]
        breakdown = jload(getattr(job, "match_breakdown_json", None), {}) or {}
        # The jobs table shows the verdict chip + top reasons without a detail fetch.
        out["verdict"] = breakdown.get("verdict")
        out["reasons"] = (breakdown.get("reasons") or [])[:3]
    return out


def job_payload(job: Any, *, description_limit: int = 12000) -> dict[str, Any]:
    """The dict shape agents (MatchRanker / ResumeArchitect) expect for a job."""
    description = getattr(job, "description", None) or ""
    return {
        "id": getattr(job, "id", None),
        "linkedin_job_id": getattr(job, "linkedin_job_id", None),
        "title": getattr(job, "title", None) or "",
        "company": getattr(job, "company", None) or "",
        "location": getattr(job, "location", None) or "",
        "url": getattr(job, "url", None) or "",
        "easy_apply": bool(getattr(job, "easy_apply", False)),
        "posted": getattr(job, "posted", None) or "",
        "description": description[:description_limit],
    }


def resume_dict(resume: Any, *, full: bool = False) -> dict[str, Any]:
    """Serialise a Resume row; markdown/breakdown only when ``full=True``."""
    out: dict[str, Any] = {
        "id": getattr(resume, "id", None),
        "profile_id": getattr(resume, "profile_id", None),
        "version": getattr(resume, "version", None),
        "label": getattr(resume, "label", None),
        "target_job_id": getattr(resume, "target_job_id", None),
        "ats_score": getattr(resume, "ats_score", None),
        "created_at": iso(getattr(resume, "created_at", None)),
        "formats": available_formats(resume),
    }
    if full:
        out["markdown"] = getattr(resume, "markdown", None) or ""
        out["ats_breakdown"] = jload(getattr(resume, "ats_breakdown_json", None), {})
        out["paths"] = {
            "docx": getattr(resume, "docx_path", None),
            "pdf": getattr(resume, "pdf_path", None),
            "txt": getattr(resume, "txt_path", None),
        }
    return out


def available_formats(resume: Any) -> list[str]:
    """Which ``?fmt=`` values ``/resumes/{id}/download`` can actually serve.

    Checked against the filesystem, not just the column, so a resume whose
    rendered artefacts were wiped from the volume does not advertise a
    download that would 404.
    """
    import os

    formats: list[str] = []
    if (getattr(resume, "markdown", None) or "").strip():
        formats.append("md")
    for fmt, attr in (("docx", "docx_path"), ("pdf", "pdf_path"), ("txt", "txt_path")):
        path = getattr(resume, attr, None)
        if path and os.path.isfile(str(path)):
            formats.append(fmt)
    return formats


def profile_dict(profile: Any, *, full: bool = False) -> dict[str, Any]:
    """Serialise a Profile row. ``raw`` is huge, so it is opt-in."""
    out: dict[str, Any] = {
        "id": getattr(profile, "id", None),
        "source": getattr(profile, "source", None),
        "linkedin_username": getattr(profile, "linkedin_username", None),
        "headline": getattr(profile, "headline", None),
        "summary": getattr(profile, "summary", None),
        "fetched_at": iso(getattr(profile, "fetched_at", None)),
        "skills": jload(getattr(profile, "skills_json", None), []),
        "experience": jload(getattr(profile, "experience_json", None), []),
        "education": jload(getattr(profile, "education_json", None), []),
        "analysis": jload(getattr(profile, "analysis_json", None), None),
    }
    if full:
        out["raw"] = jload(getattr(profile, "raw_json", None), {})
    return out


def is_terminal_run_status(status: Any) -> bool:
    """True when a Run will produce no further events."""
    return str(status or "") in _TERMINAL_RUN_STATUSES


def paginate(items: Sequence[Any], limit: int, offset: int) -> Sequence[Any]:
    """Defensive in-memory slice (used where SQL-side paging is not worth it)."""
    if offset:
        items = items[offset:]
    if limit:
        items = items[:limit]
    return items


def as_bool(value: Any, default: bool = False) -> bool:
    """Parse the many truthy spellings that arrive from query strings/settings."""
    if isinstance(value, bool):
        return value
    if value is None:
        return default
    text = str(value).strip().lower()
    if text in ("1", "true", "yes", "y", "on"):
        return True
    if text in ("0", "false", "no", "n", "off", ""):
        return False
    return default


def csv_list(value: Any) -> list[str]:
    """Split a comma-separated string (or pass a list through) into clean items."""
    if value is None:
        return []
    if isinstance(value, (list, tuple, set)):
        items: Iterable[Any] = value
    else:
        items = str(value).split(",")
    return [str(item).strip() for item in items if str(item).strip()]


def mapping_or_self(payload: Mapping[str, Any], key: str = "settings") -> Mapping[str, Any]:
    """Accept both ``{"settings": {...}}`` and a bare ``{...}`` request body."""
    inner = payload.get(key)
    if isinstance(inner, Mapping):
        return inner
    return payload
