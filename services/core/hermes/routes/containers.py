"""``/api/containers*`` + ``/api/sandbox/exec`` — the Docker control plane.

Everything here talks to ``SandboxManager`` (hermes/sandbox.py), which owns the
docker-py client. Two rules shape the module:

1. **Docker is optional.** If the socket was never mounted, or dockerd is not
   running, every endpoint answers ``503`` with the actual remediation steps
   (``SandboxUnavailableError`` already carries them) instead of a 500.

2. **Hermes must not kill itself.** Stopping, restarting or removing the
   ``hermes-core`` container would tear down the very process serving the
   request: the HTTP call could never answer, and on a non-restarting deploy
   the dashboard would be left talking to nothing. Those three actions are
   refused with ``409`` and the host-side command to use instead. Detection is
   deliberately broad — container id from ``/proc/self/cgroup`` +
   ``/proc/self/mountinfo``, the container hostname, and the pinned
   ``container_name: hermes-core`` from docker-compose.yml.

All docker calls are blocking, so they run in the threadpool. The log stream is
already async (``SandboxManager.logs_stream``) and is framed with
``events.sse_format`` so a log line that happens to start with ``data:`` cannot
corrupt the stream.
"""

from __future__ import annotations

import logging
import re
import socket
from functools import lru_cache
from pathlib import Path
from typing import Any, AsyncIterator, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import StreamingResponse
from sqlalchemy.orm import Session
from starlette.concurrency import run_in_threadpool

from hermes.db import get_db
from hermes.events import sse_format
from hermes.routes._common import SSE_HEADERS, run_dict, sse_stream
from hermes.runner import UnknownRunKind, run_and_wait
from hermes.sandbox import SandboxManager, SandboxUnavailableError, get_sandbox
from hermes.schemas import SandboxExecRequest
from hermes.settings import settings

log = logging.getLogger("hermes.api.containers")

router = APIRouter(tags=["containers"])

#: Container names that must never be stopped/restarted/removed through the API.
#: docker-compose.yml pins ``container_name: hermes-core``.
_PROTECTED_NAMES: frozenset[str] = frozenset({"hermes-core"})

#: Actions refused against this service's own container.
_SELF_DESTRUCTIVE = ("stop", "restart", "remove")

#: docker-py's "no such container" surfaces through SandboxUnavailableError.
_NO_SUCH_RE = re.compile(r"no such container", re.IGNORECASE)

#: 64-hex container ids as they appear in cgroup / mountinfo paths.
_CONTAINER_ID_RE = re.compile(r"\b([0-9a-f]{64})\b")

#: A hostname that is really a container short id.
_SHORT_ID_RE = re.compile(r"^[0-9a-f]{12,64}$")

#: Upper bound on how long ``POST /sandbox/exec`` blocks. The dashboard client
#: gives up at 600s (see src/lib/api.ts), so stay under that and let the Run row
#: carry the outcome if the sandbox is slower.
_MAX_INLINE_WAIT = 570.0


# --------------------------------------------------------------------------- #
# local helpers (self-contained per route module, matching routes/health.py)
# --------------------------------------------------------------------------- #


def _run_response(run: Any) -> dict[str, Any]:
    """``run_dict`` plus the ``*_json`` aliases the dashboard client reads."""
    payload = run_dict(run)
    payload["params_json"] = payload.get("params")
    payload["result_json"] = payload.get("result")
    return payload


def _unavailable(exc: Exception) -> HTTPException:
    return HTTPException(status_code=503, detail=f"Docker is unavailable: {exc}")


def _docker_error(exc: Exception, container_id: Optional[str] = None) -> HTTPException:
    """Map a docker/sandbox exception onto the right HTTP status."""
    text = str(exc)
    name = type(exc).__name__
    if _NO_SUCH_RE.search(text) or name in ("NotFound", "ImageNotFound"):
        return HTTPException(
            status_code=404,
            detail=(
                f"No such container: {container_id or 'unknown'}. It may already have "
                "been removed — reload GET /api/containers."
            ),
        )
    if isinstance(exc, SandboxUnavailableError):
        return _unavailable(exc)
    if name in ("DockerException", "APIError", "ConnectionError", "ReadTimeout"):
        return HTTPException(
            status_code=502,
            detail=f"The Docker daemon rejected the request ({name}): {text}",
        )
    return HTTPException(
        status_code=500, detail=f"Unexpected Docker failure ({name}): {text}"
    )


async def _docker(fn: Any, *args: Any, container_id: Optional[str] = None) -> Any:
    """Run a blocking SandboxManager call, translating failures into HTTP errors."""
    try:
        return await run_in_threadpool(fn, *args)
    except HTTPException:
        raise
    except Exception as exc:
        raise _docker_error(exc, container_id) from exc


# --------------------------------------------------------------------------- #
# "is this me?" detection
# --------------------------------------------------------------------------- #


def _read_text(path: str) -> str:
    try:
        return Path(path).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


@lru_cache(maxsize=1)
def _self_identity() -> tuple[frozenset[str], frozenset[str]]:
    """``(container ids, container names)`` that identify *this* process.

    Sources, in order of reliability:

    * ``/proc/self/mountinfo`` — contains ``/docker/containers/<64-hex>/`` for
      the container's own hosts/hostname/resolv.conf bind mounts. Works on
      cgroup v2, where ``/proc/self/cgroup`` no longer carries the id.
    * ``/proc/self/cgroup`` — the classic cgroup v1 ``/docker/<64-hex>`` path.
    * ``socket.gethostname()`` — Docker's default hostname is the 12-char short
      id; compose deployments may instead set it to the service name.
    * ``_PROTECTED_NAMES`` — docker-compose.yml pins the API container's name,
      so the guard still holds when none of the above is readable (e.g. Hermes
      running on the host against the daemon's socket).
    """
    ids: set[str] = set()
    names: set[str] = set(_PROTECTED_NAMES)

    for source in ("/proc/self/mountinfo", "/proc/self/cgroup"):
        for match in _CONTAINER_ID_RE.finditer(_read_text(source)):
            ids.add(match.group(1).lower())

    try:
        hostname = (socket.gethostname() or "").strip().lower()
    except Exception:  # pragma: no cover - gethostname does not normally fail
        hostname = ""
    if hostname:
        if _SHORT_ID_RE.match(hostname):
            ids.add(hostname)
        else:
            names.add(hostname)

    log.debug("self-identity: ids=%s names=%s", sorted(ids), sorted(names))
    return frozenset(ids), frozenset(names)


def _is_self(container: dict[str, Any]) -> bool:
    """True when ``container`` is the container this API is running in."""
    ids, names = _self_identity()
    name = str(container.get("name") or "").lstrip("/").lower()
    if name and name in names:
        return True
    cid = str(container.get("id") or "").lower()
    if not cid:
        return False
    for token in ids:
        # Compare on the short-id prefix: hostnames give 12 chars, cgroup 64.
        length = min(len(token), len(cid), 64)
        if length >= 12 and token[:length] == cid[:length]:
            return True
    return False


def _refuse_self(container: dict[str, Any], action: str) -> None:
    """409 when ``action`` would tear down the container serving this request."""
    if action not in _SELF_DESTRUCTIVE or not _is_self(container):
        return
    name = str(container.get("name") or container.get("id") or "hermes-core")
    raise HTTPException(
        status_code=409,
        detail=(
            f"Refusing to {action} {name}: that is the hermes-core container serving "
            "this request, so the API would die mid-call and could never report the "
            f"result. Run it from the host instead — `docker {action if action != 'remove' else 'rm'} "
            f"{name}` (or `make down` / `make up`). Every other container on the "
            "Containers page can be managed from here."
        ),
    )


# --------------------------------------------------------------------------- #
# container lookup
# --------------------------------------------------------------------------- #


def _container_payload(container: dict[str, Any]) -> dict[str, Any]:
    """Add the dashboard-facing flags to a ``SandboxManager`` container dict."""
    payload = dict(container)
    payload["is_self"] = _is_self(container)
    payload["protected"] = payload["is_self"]
    return payload


async def _resolve(sandbox: SandboxManager, container_id: str) -> dict[str, Any]:
    """Find one container by full id, short-id prefix or name.

    Uses ``list_containers()`` (a single daemon call) rather than a bare
    ``inspect`` so the guardrail can see the container's *name* and labels
    before mutating it.
    """
    needle = (container_id or "").strip().lower()
    if not needle:
        raise HTTPException(status_code=422, detail="A container id or name is required.")

    containers: list[dict[str, Any]] = await _docker(
        sandbox.list_containers, True, container_id=container_id
    )
    for container in containers:
        cid = str(container.get("id") or "").lower()
        name = str(container.get("name") or "").lstrip("/").lower()
        if needle == cid or needle == name or (len(needle) >= 8 and cid.startswith(needle)):
            return container

    raise HTTPException(
        status_code=404,
        detail=(
            f"No such container: {container_id!r}. GET /api/containers lists what the "
            "Docker daemon can see."
        ),
    )


# --------------------------------------------------------------------------- #
# GET /containers
# --------------------------------------------------------------------------- #


@router.get("/containers", summary="Every container the Docker daemon can see")
async def list_containers(
    all_containers: bool = Query(
        True, alias="all", description="Include stopped containers (default true)."
    ),
) -> dict[str, Any]:
    """The container inventory, newest first.

    Not filtered to the Hermes stack: the Containers page manages the whole
    compose project (freellmapi, linkedin-mcp, hermes-core, hermes-dashboard)
    plus ephemeral sandboxes. Each row carries ``labels``, ``is_hermes`` and
    ``is_self`` so the UI can group them and grey out the protected one.
    """
    sandbox = get_sandbox()
    containers: list[dict[str, Any]] = await _docker(
        sandbox.list_containers, bool(all_containers)
    )
    return {
        "items": [_container_payload(c) for c in containers],
        "total": len(containers),
        "docker_ok": True,
        "detail": "ok",
    }


# --------------------------------------------------------------------------- #
# POST /containers/{id}/start|stop|restart, DELETE /containers/{id}
# --------------------------------------------------------------------------- #


@router.post("/containers/{container_id}/start", summary="Start a container")
async def start_container(container_id: str) -> dict[str, Any]:
    sandbox = get_sandbox()
    container = await _resolve(sandbox, container_id)
    cid = str(container.get("id"))
    result = await _docker(sandbox.start, cid, container_id=container_id)
    log.info("started container %s (%s)", container.get("name"), cid[:12])
    return {
        **_container_payload(result),
        "action": "start",
        "detail": f"Started {container.get('name') or cid[:12]}.",
    }


@router.post("/containers/{container_id}/stop", summary="Stop a container")
async def stop_container(
    container_id: str,
    timeout: int = Query(10, ge=0, le=600, description="Grace period before SIGKILL."),
) -> dict[str, Any]:
    sandbox = get_sandbox()
    container = await _resolve(sandbox, container_id)
    _refuse_self(container, "stop")
    cid = str(container.get("id"))
    result = await _docker(sandbox.stop, cid, int(timeout), container_id=container_id)
    log.info("stopped container %s (%s)", container.get("name"), cid[:12])
    return {
        **_container_payload(result),
        "action": "stop",
        "detail": f"Stopped {container.get('name') or cid[:12]}.",
    }


@router.post("/containers/{container_id}/restart", summary="Restart a container")
async def restart_container(
    container_id: str,
    timeout: int = Query(10, ge=0, le=600, description="Grace period before SIGKILL."),
) -> dict[str, Any]:
    sandbox = get_sandbox()
    container = await _resolve(sandbox, container_id)
    # A self-restart stops this process first: the response could never be sent.
    _refuse_self(container, "restart")
    cid = str(container.get("id"))
    result = await _docker(sandbox.restart, cid, int(timeout), container_id=container_id)
    log.info("restarted container %s (%s)", container.get("name"), cid[:12])
    return {
        **_container_payload(result),
        "action": "restart",
        "detail": f"Restarted {container.get('name') or cid[:12]}.",
    }


@router.delete("/containers/{container_id}", summary="Remove a container")
async def remove_container(
    container_id: str,
    force: bool = Query(False, description="Kill it first if it is still running."),
) -> dict[str, Any]:
    """Remove a container. Named volumes (the LinkedIn session!) are preserved."""
    sandbox = get_sandbox()
    container = await _resolve(sandbox, container_id)
    _refuse_self(container, "remove")
    cid = str(container.get("id"))
    name = str(container.get("name") or cid[:12])
    running = str(container.get("state") or container.get("status") or "").lower()
    if running.startswith("running") and not force:
        raise HTTPException(
            status_code=409,
            detail=(
                f"{name} is still running. Stop it first "
                f"(POST /api/containers/{cid}/stop), or repeat this call with "
                "?force=true to kill and remove it."
            ),
        )
    result = await _docker(sandbox.remove, cid, bool(force), container_id=container_id)
    log.info("removed container %s (%s, force=%s)", name, cid[:12], bool(force))
    return {
        **result,
        "action": "remove",
        "detail": (
            f"Removed {name}. Named volumes (including the LinkedIn session) were "
            "left untouched; `make up` will recreate the container."
        ),
    }


# --------------------------------------------------------------------------- #
# GET /containers/{id}/stats
# --------------------------------------------------------------------------- #


@router.get("/containers/{container_id}/stats", summary="One-shot resource snapshot")
async def container_stats(container_id: str) -> dict[str, Any]:
    """CPU / memory / network for one container (a single docker stats sample)."""
    sandbox = get_sandbox()
    container = await _resolve(sandbox, container_id)
    cid = str(container.get("id"))
    state = str(container.get("state") or container.get("status") or "").lower()
    if not state.startswith("running"):
        # docker returns all-zero stats for a stopped container; say so instead.
        return {
            "id": cid,
            "name": container.get("name"),
            "cpu_percent": 0.0,
            "mem_usage_mb": 0.0,
            "mem_limit_mb": 0.0,
            "mem_percent": 0.0,
            "net_rx": 0,
            "net_tx": 0,
            "pids": 0,
            "running": False,
            "detail": f"{container.get('name') or cid[:12]} is not running ({state or 'unknown'}).",
        }
    stats = await _docker(sandbox.stats, cid, container_id=container_id)
    return {**stats, "running": True, "detail": "ok"}


# --------------------------------------------------------------------------- #
# GET /containers/{id}/logs  (SSE)
# --------------------------------------------------------------------------- #


@router.get("/containers/{container_id}/logs", summary="Follow a container's logs (SSE)")
async def container_logs(
    container_id: str,
    request: Request,
    tail: int = Query(200, ge=0, le=10000, description="Lines of history to replay."),
) -> StreamingResponse:
    """Live ``text/event-stream`` of one container's stdout+stderr.

    Each frame is one log line. Frames are pre-framed with ``events.sse_format``
    so a line beginning with ``data:`` (or ``:``) cannot be mistaken for stream
    syntax, and multi-line output is split across ``data:`` lines as the SSE
    spec requires.
    """
    sandbox = get_sandbox()
    container = await _resolve(sandbox, container_id)
    cid = str(container.get("id"))
    name = str(container.get("name") or cid[:12])

    async def _lines() -> AsyncIterator[str]:
        async for line in sandbox.logs_stream(cid, tail=int(tail)):
            yield sse_format(line)

    async def _factory() -> AsyncIterator[str]:
        return _lines()

    generator = sse_stream(
        _factory,
        request=request,
        replay=[sse_format(f"[hermes] attached to {name} ({cid[:12]}), tail={tail}")],
        final={"level": "end", "message": f"[hermes] log stream for {name} ended"},
    )
    return StreamingResponse(
        generator,
        media_type="text/event-stream",
        headers=SSE_HEADERS,
    )


# --------------------------------------------------------------------------- #
# POST /sandbox/exec
# --------------------------------------------------------------------------- #


@router.post("/sandbox/exec", summary="Run Python in an ephemeral hardened sandbox")
async def sandbox_exec(
    payload: SandboxExecRequest,
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    """Execute code in a throwaway container and return the Run *and* its result.

    The call blocks until the sandbox exits (the contract's response carries the
    ``SandboxResult``), bounded by the request's ``timeout`` plus a grace period
    and hard-capped below the dashboard client's own 600s limit. Every run is
    also recorded as a Run row, so a call that outlives the HTTP request can
    still be followed at ``GET /api/runs/{id}/events``.

    The container is network-disabled, read-only-rootfs, non-root and
    resource-capped (see ``settings.sandbox_limits()``); only ``/work`` is
    writable and it is deleted afterwards.
    """
    sandbox = get_sandbox()
    if not await run_in_threadpool(sandbox.docker_available):
        raise HTTPException(
            status_code=503,
            detail=(
                f"Cannot reach the Docker daemon at {sandbox.docker_host!r}, so no "
                "sandbox can be started. Check that dockerd / Docker Desktop is "
                "running and that hermes-core mounts the socket "
                "(/var/run/docker.sock)."
            ),
        )

    limits = settings.sandbox_limits()
    default_timeout = int(limits.get("timeout_s") or 300)
    timeout_s = int(payload.timeout or default_timeout)
    inline_wait = min(float(timeout_s) + 30.0, _MAX_INLINE_WAIT)

    params: dict[str, Any] = {"code": payload.code}
    if payload.files:
        params["files"] = dict(payload.files)
    if payload.timeout is not None:
        params["timeout"] = int(payload.timeout)
    if payload.network is not None:
        params["network"] = str(payload.network)

    try:
        run = await run_and_wait(db, "sandbox_exec", params, timeout=inline_wait)
    except UnknownRunKind as exc:  # pragma: no cover - guarded by RUN_KINDS
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(
            status_code=422, detail=f"Cannot start the sandbox run: {exc}"
        ) from exc
    except RuntimeError as exc:
        raise HTTPException(
            status_code=500, detail=f"Cannot start the sandbox run: {exc}"
        ) from exc

    run_payload = _run_response(run)
    raw = run_payload.get("result") or {}
    result: Optional[dict[str, Any]] = None
    if isinstance(raw, dict) and "exit_code" in raw:
        result = {
            "exit_code": raw.get("exit_code", -1),
            "stdout": raw.get("stdout") or "",
            "stderr": raw.get("stderr") or "",
            "artifacts": list(raw.get("artifacts") or []),
            "container_id": raw.get("container_id") or "",
            "timed_out": bool(raw.get("timed_out")),
            "duration_s": raw.get("duration_s"),
            "workspace": raw.get("workspace") or "",
        }

    if result is not None:
        detail = f"Sandbox exited with code {result['exit_code']}."
        if result["timed_out"]:
            detail = f"Sandbox was killed after {timeout_s}s (timeout)."
    elif run_payload.get("status") == "error":
        detail = run_payload.get("error") or "The sandbox run failed."
    else:
        detail = (
            f"The sandbox did not finish within {inline_wait:.0f}s. Follow "
            f"GET /api/runs/{run_payload['id']}/events for the rest of the output."
        )

    return {
        # Top-level Run fields: src/lib/types.ts models this response as
        # `SandboxExecResponse extends Run`, and also accepts {run, result}.
        "id": run_payload.get("id"),
        "kind": run_payload.get("kind"),
        "status": run_payload.get("status"),
        "error": run_payload.get("error"),
        "started_at": run_payload.get("started_at"),
        "finished_at": run_payload.get("finished_at"),
        "run": run_payload,
        "result": result,
        "detail": detail,
    }
