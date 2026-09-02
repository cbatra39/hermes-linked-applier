"""``/api/runs`` — run history and the live SSE event stream.

Every long job in Hermes (profile import, resume build, job search, tailoring,
sandbox exec) is a ``Run`` row plus a stream of ``RunEvent`` rows. The dashboard
starts a run through the relevant domain endpoint, then follows it here.

``GET /api/runs/{id}/events`` is the only endpoint in Hermes that stays open for
minutes at a time. Three things make that safe:

* **History is replayed first.** A client that connects late (or reconnects
  after a page reload) still gets the events it missed, because they are
  persisted rows — the in-memory bus is only used for the live tail.
* **The stream closes itself.** ``stop_check`` polls the run's status on each
  keepalive tick, so a finished run does not leave the browser holding an
  EventSource open forever. The closing payload is a ``run_complete`` marker;
  the UI then re-fetches ``GET /api/runs/{id}`` for the authoritative result.
* **Idle connections are not silently dropped.** ``sse_stream`` emits keepalive
  comments; nginx is configured with ``proxy_buffering off`` so they arrive.
"""

from __future__ import annotations

import logging
from typing import Any, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from sqlalchemy import func, select
from sqlalchemy.orm import Session
from starlette.concurrency import run_in_threadpool
from starlette.responses import StreamingResponse

from hermes.db import SessionLocal, get_db
from hermes.models import Run, RunEvent
from hermes.routes._common import (
    SSE_HEADERS,
    coerce_pk,
    event_dict,
    is_terminal_run_status,
    run_dict,
    sse_pack,
    sse_stream,
    subscribe_iter,
)

log = logging.getLogger("hermes.api.runs")

router = APIRouter(tags=["runs"])

#: Cap on replayed history so a pathological run cannot flood a reconnecting tab.
_MAX_REPLAY_EVENTS = 500


def _load_run(db: Session, run_id: str) -> Run:
    run = db.get(Run, coerce_pk(Run, run_id))
    if run is None:
        raise HTTPException(status_code=404, detail=f"No run with id {run_id!r}.")
    return run


@router.get("/runs", summary="List runs, newest first")
def list_runs(
    kind: Optional[str] = Query(None, description="Filter by run kind."),
    status: Optional[str] = Query(None, description="Filter by run status."),
    limit: int = Query(50, ge=1, le=500),
    offset: int = Query(0, ge=0),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    """Run history for the Runs page."""
    filters = []
    if kind:
        filters.append(Run.kind == kind)
    if status:
        filters.append(Run.status == status)

    total = db.execute(select(func.count()).select_from(Run).where(*filters)).scalar_one()
    stmt = (
        select(Run)
        .where(*filters)
        .order_by(Run.started_at.desc())
        .limit(limit)
        .offset(offset)
    )
    runs = list(db.execute(stmt).scalars())

    return {
        "runs": [run_dict(r) for r in runs],
        "total": total,
        "limit": limit,
        "offset": offset,
    }


@router.get("/runs/{run_id}", summary="One run, with its event log")
def get_run(
    run_id: str,
    events: bool = Query(True, description="Include the persisted event log."),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    """A single run. Used for the detail drawer and for polling fallbacks."""
    run = _load_run(db, run_id)
    if not events:
        return run_dict(run)

    rows = list(
        db.execute(
            select(RunEvent)
            .where(RunEvent.run_id == run.id)
            .order_by(RunEvent.id.asc())
            .limit(_MAX_REPLAY_EVENTS)
        ).scalars()
    )
    return run_dict(run, events=rows)


@router.get("/runs/{run_id}/events", summary="Live run events (SSE)")
async def stream_run_events(run_id: str, request: Request) -> StreamingResponse:
    """
    Server-sent events for one run: persisted history, then the live tail.

    Opens its own short-lived sessions rather than taking ``Depends(get_db)``,
    because a FastAPI dependency session would be held open for the entire life
    of the stream — minutes, on a SQLite connection pool.
    """

    def _load_head() -> tuple[dict[str, Any], list[dict[str, Any]], bool]:
        with SessionLocal() as db:
            run = db.get(Run, coerce_pk(Run, run_id))
            if run is None:
                raise HTTPException(status_code=404, detail=f"No run with id {run_id!r}.")
            rows = list(
                db.execute(
                    select(RunEvent)
                    .where(RunEvent.run_id == run.id)
                    .order_by(RunEvent.id.asc())
                    .limit(_MAX_REPLAY_EVENTS)
                ).scalars()
            )
            return run_dict(run), [event_dict(e) for e in rows], is_terminal_run_status(run.status)

    snapshot, history, already_done = await run_in_threadpool(_load_head)

    def _current_status() -> Any:
        with SessionLocal() as db:
            run = db.get(Run, coerce_pk(Run, run_id))
            return run.status if run is not None else snapshot.get("status")

    async def _is_finished() -> bool:
        return is_terminal_run_status(await run_in_threadpool(_current_status))

    if already_done:
        # Nothing more will ever be published for this run: flush history plus
        # the terminal run object and close, instead of holding a dead stream.
        async def _closed_stream():
            for event in history:
                yield sse_pack(event)
            yield sse_pack({"event": "run", "run": snapshot})

        return StreamingResponse(
            _closed_stream(), media_type="text/event-stream", headers=dict(SSE_HEADERS)
        )

    # `replay` payloads are packed by sse_stream itself, so hand it raw dicts —
    # pre-packing here would emit `data: "data: {...}"`.
    #
    # `final` is a static payload (sse_stream packs it verbatim), so it cannot
    # carry the run's closing state — that is not known until stop_check fires.
    # Emit a completion marker instead and let the UI re-fetch
    # GET /api/runs/{id}, which is one cheap call and always authoritative.
    body = sse_stream(
        lambda: subscribe_iter(run_id),
        request=request,
        replay=history,
        keepalive=15.0,
        stop_check=_is_finished,
        final={"event": "run_complete", "run_id": run_id},
    )
    return StreamingResponse(body, media_type="text/event-stream", headers=dict(SSE_HEADERS))
