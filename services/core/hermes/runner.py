"""Run scheduling: create a ``Run`` row and drive the matching pipeline.

Two entry points:

* :func:`start_run` — fire-and-forget. Creates the row, schedules the pipeline
  coroutine on the running event loop, returns immediately so the HTTP handler
  can hand the client a Run id to follow over SSE.
* :func:`run_and_wait` — same, but awaits completion and returns the finished
  row. Used by ``POST /api/sandbox/exec``, which the contract says returns a Run
  *and* the SandboxResult.

**A scheduled run may never die silently.** Three layers guarantee that:

1. ``pipeline._run_scope`` records success/failure on the row.
2. :func:`_supervise` catches anything that escapes the pipeline (an import
   error, a bug in the pipeline's own error handling) and writes it to
   ``Run.error``.
3. :func:`_on_task_done` inspects the finished task and logs any exception the
   supervisor somehow missed, so nothing is swallowed by the event loop.

A module-level strong reference set keeps tasks alive; without it CPython is
free to garbage-collect a running task, which manifests as runs that silently
stop halfway.
"""

from __future__ import annotations

import asyncio
import json
import logging
import uuid
from typing import Any, Awaitable, Callable

from sqlalchemy.orm import Session

from hermes import pipeline
from hermes.models import Run
from hermes.routes._common import RUN_KINDS, emit, jdump, pk_python_type, pk_value, utcnow

log = logging.getLogger("hermes.runner")

__all__ = ["start_run", "run_and_wait", "active_runs", "shutdown_runs", "DISPATCH"]

#: Strong references to in-flight tasks (see module docstring).
_TASKS: set[asyncio.Task[Any]] = set()

#: kind -> coroutine factory. Every kind in the contract's Run.kind domain is
#: present; a missing key is a 400 at the HTTP layer, never a silent no-op.
DISPATCH: dict[str, Callable[[str, dict[str, Any]], Awaitable[Any]]] = {
    "profile_import": lambda rid, p: pipeline.run_profile_import(rid, p.get("linkedin_username")),
    "job_search": lambda rid, p: pipeline.run_job_search(rid, p),
    "resume_build": lambda rid, p: pipeline.run_resume_build(
        rid, p.get("profile_id"), p.get("target_job_id")
    ),
    "job_tailor": lambda rid, p: pipeline.run_job_tailor(rid, p.get("job_id")),
    "ats_score": lambda rid, p: pipeline.run_ats_score(rid, p.get("resume_id"), p.get("job_id")),
    "sandbox_exec": lambda rid, p: pipeline.run_sandbox_exec(
        rid,
        p.get("code") or "",
        p.get("files"),
        p.get("timeout"),
        p.get("network"),
    ),
    "full_pipeline": lambda rid, p: pipeline.run_full_pipeline(rid, p),
}

# Sanity check at import time: a typo here would only surface when a user
# clicks the button, which is far too late.
assert set(DISPATCH) == set(RUN_KINDS), (
    f"DISPATCH is out of sync with the Run.kind domain: "
    f"missing={set(RUN_KINDS) - set(DISPATCH)}, extra={set(DISPATCH) - set(RUN_KINDS)}"
)


class UnknownRunKind(ValueError):
    """Raised when a caller asks for a run kind Hermes cannot execute."""


# --------------------------------------------------------------------------- #
# Row creation
# --------------------------------------------------------------------------- #


def _needs_generated_id() -> bool:
    """True when ``Run.id`` is a string column with no default we can rely on.

    The build contract fixes the *columns* of ``Run`` but not the type of ``id``,
    while ``bus.publish(run_id, ...)`` and ``LLMRouter.chat(run_id=...)`` are
    typed ``str``. Supporting both shapes here keeps this module correct whether
    ``models.py`` chose autoincrement integers or UUID strings.
    """
    if pk_python_type(Run) is int:
        return False
    try:
        column = Run.__table__.c["id"]
    except Exception:  # pragma: no cover
        return True
    return column.default is None and column.server_default is None


def _sanitise_params(params: dict[str, Any] | None) -> dict[str, Any]:
    """Ensure params round-trip through ``params_json`` without surprises."""
    payload = dict(params or {})
    try:
        json.dumps(payload, default=str)
    except (TypeError, ValueError) as exc:  # pragma: no cover - defensive
        raise ValueError(f"Run params are not JSON-serialisable: {exc}") from exc
    return payload


def _create_run_row(db: Session, kind: str, params: dict[str, Any]) -> Run:
    run = Run(
        kind=kind,
        status="pending",
        params_json=jdump(params),
        started_at=utcnow(),
    )
    if _needs_generated_id():
        run.id = uuid.uuid4().hex
    db.add(run)
    db.commit()
    db.refresh(run)
    return run


# --------------------------------------------------------------------------- #
# Supervision
# --------------------------------------------------------------------------- #


async def _record_unhandled(run_id: str, kind: str, exc: BaseException) -> None:
    """Last-resort failure recording (used when the pipeline itself blew up)."""
    detail = f"{type(exc).__name__}: {exc}"
    await emit(run_id, "error", f"{kind}: unhandled failure — {detail}")
    try:
        await pipeline.db_call(_mark_error, run_id, detail)
    except Exception:  # pragma: no cover - DB itself is down
        log.exception("could not record failure for run %s", run_id)


def _mark_error(db: Session, run_id: str, detail: str) -> None:
    run = db.get(Run, pk_value(Run, run_id))
    if run is None:
        return
    # Do not overwrite a specific error the pipeline already recorded.
    if run.status != "error":
        run.status = "error"
        run.error = detail[:4000]
    if run.finished_at is None:
        run.finished_at = utcnow()


async def _supervise(kind: str, run_id: str, params: dict[str, Any]) -> Any:
    """Await the pipeline coroutine, converting escapes into recorded errors."""
    factory = DISPATCH[kind]
    try:
        return await factory(run_id, params)
    except asyncio.CancelledError:
        log.info("run %s (%s) cancelled", run_id, kind)
        await _record_unhandled(run_id, kind, RuntimeError("cancelled"))
        raise
    except Exception as exc:
        log.exception("unhandled exception escaped pipeline for run %s (%s)", run_id, kind)
        await _record_unhandled(run_id, kind, exc)
        return {"error": str(exc)}


def _on_task_done(task: asyncio.Task[Any]) -> None:
    """Drop the strong reference and make sure nothing is swallowed."""
    _TASKS.discard(task)
    if task.cancelled():
        return
    exc = task.exception()
    if exc is not None:  # pragma: no cover - _supervise should have caught it
        log.error("run task finished with an unretrieved exception: %r", exc, exc_info=exc)


def _schedule(kind: str, run_id: str, params: dict[str, Any]) -> asyncio.Task[Any]:
    try:
        asyncio.get_running_loop()
    except RuntimeError as exc:  # pragma: no cover - misuse from sync context
        raise RuntimeError(
            "start_run() must be called from an async context (an `async def` "
            "FastAPI endpoint); there is no running event loop here."
        ) from exc
    task = asyncio.create_task(_supervise(kind, run_id, params), name=f"hermes-run-{kind}-{run_id}")
    _TASKS.add(task)
    task.add_done_callback(_on_task_done)
    return task


# --------------------------------------------------------------------------- #
# Public API
# --------------------------------------------------------------------------- #


def start_run(db: Session, kind: str, params: dict[str, Any] | None = None) -> Run:
    """Create a Run row for ``kind`` and schedule its pipeline.

    Returns the persisted (refreshed) Run so the caller can serialise it
    straight into the HTTP response. Must be called with a running event loop.
    """
    if kind not in DISPATCH:
        raise UnknownRunKind(
            f"Unknown run kind {kind!r}. Valid kinds: {', '.join(sorted(DISPATCH))}."
        )
    payload = _sanitise_params(params)
    run = _create_run_row(db, kind, payload)
    _schedule(kind, str(run.id), payload)
    log.info("scheduled run %s kind=%s params=%s", run.id, kind, sorted(payload))
    return run


async def run_and_wait(
    db: Session,
    kind: str,
    params: dict[str, Any] | None = None,
    timeout: float | None = None,
) -> Run:
    """Create + execute a run inline and return the completed row.

    On timeout the underlying task is cancelled and the row is left in whatever
    state the pipeline reached (``error`` once the cancellation is recorded).
    """
    if kind not in DISPATCH:
        raise UnknownRunKind(
            f"Unknown run kind {kind!r}. Valid kinds: {', '.join(sorted(DISPATCH))}."
        )
    payload = _sanitise_params(params)
    run = _create_run_row(db, kind, payload)
    task = _schedule(kind, str(run.id), payload)
    try:
        if timeout is not None:
            await asyncio.wait_for(asyncio.shield(task), timeout=timeout)
        else:
            await asyncio.shield(task)
    except asyncio.TimeoutError:
        task.cancel()
        log.warning("run %s (%s) exceeded the %.0fs inline timeout", run.id, kind, timeout or 0)
        await _record_unhandled(
            str(run.id), kind, TimeoutError(f"inline execution exceeded {timeout:.0f}s")
        )
    db.expire_all()
    refreshed = db.get(Run, pk_value(Run, run.id))
    return refreshed or run


def active_runs() -> list[dict[str, Any]]:
    """In-flight run tasks (diagnostics for ``GET /api/health`` and logs)."""
    return [
        {"name": task.get_name(), "done": task.done(), "cancelled": task.cancelled()}
        for task in list(_TASKS)
    ]


async def shutdown_runs(grace: float = 5.0) -> None:
    """Cancel in-flight runs on service shutdown and let them record the fact."""
    tasks = [task for task in list(_TASKS) if not task.done()]
    if not tasks:
        return
    log.info("cancelling %d in-flight run(s) on shutdown", len(tasks))
    for task in tasks:
        task.cancel()
    # Bounded wait: a wedged sandbox must not block container shutdown forever.
    await asyncio.wait(tasks, timeout=grace)
