"""``GET /api/linkedin/status`` / ``POST /api/linkedin/login``.

WHY THIS FILE LOOKS MORE COMPLICATED THAN "call mcp.health()"
-------------------------------------------------------------
``LinkedInMCP.health(probe_auth=True)`` drives a real Chromium instance. A warm
session answers in a second or two, an invalid session errors out immediately —
but a *cold* container takes up to ~90 s to open the browser and load the top
card. The dashboard polls this endpoint with a 30 s client timeout, so probing
inline would guarantee an aborted request (and a half-finished scrape) every
time the container had just started.

So the probe runs as a **single background task** whose result is cached here,
and the request path only ever waits a couple of seconds for it:

* cached answer available -> return it (flagged ``stale`` once past the TTL);
* probe finishes inside ``_FAST_WAIT_S`` -> return the real answer (this covers
  the two cases that matter most, "not logged in" and "container down", both of
  which fail fast);
* otherwise -> ``status: "checking"``, HTTP 200, and the next poll gets it.

``login_required`` is the flag the UI should key "LinkedIn not connected" off:
it is true *only* for a confirmed unauthenticated session, never for "we could
not find out yet".

``POST /linkedin/login`` returns instructions, not an action. Hermes does not
type anyone's password, does not handle 2FA and does not solve captchas — the
human signs in by hand in the noVNC viewer. And even with a valid session there
is no apply/submit tool on the MCP server: Hermes ranks jobs and hands back an
apply URL for a human to click.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

from fastapi import APIRouter, HTTPException, Query

from hermes.mcp_client import LinkedInMCP, MCPError, get_mcp
from hermes.settings import ConfigError, settings

log = logging.getLogger("hermes.api.linkedin")

#: No prefix + full paths in the decorators, matching routes/health.py.
router = APIRouter(tags=["linkedin"])

#: How long a probe result is served without re-probing.
STATUS_TTL_S = 45.0

#: How long a request will wait for an in-flight probe before answering
#: "checking". Kept well under the dashboard's 30 s client timeout.
_FAST_WAIT_S = 4.0

#: Statuses that mean "the human must sign in".
_LOGIN_REQUIRED_STATUSES = frozenset({"unauthenticated"})

#: The exact text the MCP server returns when the session volume is empty.
#: Reproduced here so the dashboard can show the operator what the server said.
NO_SESSION_MARKER = (
    "No valid LinkedIn session is available in Docker. Create one with the explicit "
    "--login --login-viewer Docker command, or run --login on the host, then retry this tool."
)

# --------------------------------------------------------------------------- #
# background probe + cache
# --------------------------------------------------------------------------- #

_cached_status: dict[str, Any] | None = None
_cached_at: float = 0.0
_probe_task: "asyncio.Task[dict[str, Any]] | None" = None


def _mcp() -> LinkedInMCP:
    """The shared MCP client, or a 503 explaining the misconfiguration."""
    try:
        settings.require_mcp()
    except ConfigError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    try:
        return get_mcp()
    except MCPError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc


async def _probe(mcp: LinkedInMCP, probe_auth: bool) -> dict[str, Any]:
    """Run one real health probe and cache it. Never raises."""
    global _cached_status, _cached_at
    try:
        info = await mcp.health(probe_auth=probe_auth, use_cache=False)
    except Exception as exc:  # noqa: BLE001 - health() should not raise, but must not kill the task
        log.warning("LinkedIn health probe raised: %s: %s", type(exc).__name__, exc)
        info = {
            "reachable": False,
            "authenticated": False,
            "status": "unknown",
            "tools": [],
            "url": getattr(mcp, "url", settings.linkedin_mcp_url),
            "detail": f"Health probe failed unexpectedly: {type(exc).__name__}: {exc}",
            "checked_at": time.time(),
        }
    if not isinstance(info, dict):  # pragma: no cover - contract says dict
        info = {
            "reachable": False,
            "authenticated": False,
            "status": "unknown",
            "detail": f"mcp.health() returned {type(info).__name__}, expected a dict.",
        }
    _cached_status, _cached_at = dict(info), time.monotonic()
    return _cached_status


def _ensure_probe(mcp: LinkedInMCP, probe_auth: bool) -> "asyncio.Task[dict[str, Any]]":
    """Start the probe unless one is already running. Returns the live task.

    Safe without a lock: there is no ``await`` between the liveness check and
    ``create_task``, so two concurrent requests cannot both start a probe.
    """
    global _probe_task
    task = _probe_task
    if task is not None and not task.done():
        return task
    task = asyncio.create_task(_probe(mcp, probe_auth), name="hermes-linkedin-health-probe")
    _probe_task = task
    task.add_done_callback(_on_probe_done)
    return task


def _on_probe_done(task: "asyncio.Task[dict[str, Any]]") -> None:
    if task.cancelled():
        log.info("LinkedIn health probe cancelled")
        return
    exc = task.exception()
    if exc is not None:  # pragma: no cover - _probe swallows everything
        log.error("LinkedIn health probe crashed: %r", exc, exc_info=exc)


def invalidate_status_cache() -> None:
    """Forget the cached probe result (used after the login flow is handed out)."""
    global _cached_status, _cached_at
    _cached_status, _cached_at = None, 0.0


def _decorate(info: dict[str, Any], *, probing: bool) -> dict[str, Any]:
    """Add the fields the dashboard needs on top of ``mcp.health()``."""
    status = str(info.get("status") or "unknown")
    tools = info.get("tools") or []
    age = None if _cached_at == 0.0 else round(time.monotonic() - _cached_at, 1)
    out: dict[str, Any] = {
        "reachable": bool(info.get("reachable")),
        "authenticated": bool(info.get("authenticated")),
        "detail": str(info.get("detail") or ""),
        "status": status,
        "url": str(info.get("url") or settings.linkedin_mcp_url),
        "tools": list(tools) if isinstance(tools, (list, tuple)) else [],
        "checked_at": info.get("checked_at"),
        # True only for a *confirmed* invalid session — the UI should show
        # "LinkedIn not connected" on this flag, not on `authenticated == false`,
        # which is also false while the first probe is still running.
        "login_required": status in _LOGIN_REQUIRED_STATUSES,
        "can_auto_login": False,  # Hermes never signs in for the user.
        "viewer_url": settings.linkedin_viewer_url,
        "login_command": "make login-linkedin",
        "login_endpoint": "/api/linkedin/login",
        "probing": probing,
        "age_s": age,
        "stale": bool(age is not None and age > STATUS_TTL_S),
    }
    out["tool_count"] = len(out["tools"])
    return out


# --------------------------------------------------------------------------- #
# routes
# --------------------------------------------------------------------------- #


@router.get("/linkedin/status", summary="LinkedIn MCP reachability + session state")
async def linkedin_status(
    probe_auth: bool = Query(True, description="Also verify the LinkedIn session (drives Chromium)."),
    refresh: bool = Query(False, description="Ignore the cached probe result and re-probe."),
) -> dict[str, Any]:
    """Reachability of the MCP container and validity of the LinkedIn session."""
    mcp = _mcp()

    if refresh:
        invalidate_status_cache()
        try:
            mcp.invalidate_health_cache()
        except Exception:  # noqa: BLE001 - best effort
            log.debug("could not invalidate the MCP health cache", exc_info=True)

    fresh_enough = (
        _cached_status is not None and (time.monotonic() - _cached_at) < STATUS_TTL_S and not refresh
    )
    if fresh_enough and _cached_status is not None:
        running = _probe_task is not None and not _probe_task.done()
        return _decorate(_cached_status, probing=running)

    task = _ensure_probe(mcp, probe_auth)
    # asyncio.wait() does NOT cancel on timeout — a slow cold-start probe keeps
    # running in the background and lands in the cache for the next poll.
    await asyncio.wait({task}, timeout=_FAST_WAIT_S)

    if _cached_status is not None:
        return _decorate(_cached_status, probing=not task.done())

    return _decorate(
        {
            "reachable": False,
            "authenticated": False,
            "status": "checking",
            "url": mcp.url,
            "tools": [],
            "detail": (
                "First LinkedIn check is still running. The MCP container has to start a "
                "headless Chromium and load a page, which takes up to ~90 s from cold; a "
                "missing session or an unreachable container fails much faster than that. "
                "Poll GET /api/linkedin/status again in a few seconds."
            ),
        },
        probing=True,
    )


@router.post("/linkedin/login", summary="How to sign in to LinkedIn by hand")
async def linkedin_login() -> dict[str, Any]:
    """Return the noVNC viewer URL and the manual sign-in procedure.

    This endpoint starts nothing and submits nothing. The login container is a
    one-shot compose service that needs a terminal on the host, and the sign-in
    itself (password, 2FA, captcha) is done by the human in the viewer.
    """
    viewer_url = settings.linkedin_viewer_url
    steps = [
        "Hermes cannot sign in for you. It never types your LinkedIn password, "
        "never handles your 2FA code and never solves captchas — that is a "
        "deliberate design decision, not a missing feature.",
        "On the Docker host, start the one-shot login container: `make login-linkedin` "
        "(Linux/macOS/WSL), or `powershell -ExecutionPolicy Bypass -File "
        "scripts\\linkedin-login.ps1` on Windows, or `bash scripts/linkedin-login.sh`.",
        f"When the container reports it is up, open {viewer_url} in your browser. That is "
        "the noVNC desktop of the scraper's own Chromium.",
        "Sign in to LinkedIn in that browser with your own hands: email, password, the 2FA "
        "code and any captcha or checkpoint LinkedIn shows you.",
        "Leave the session on the LinkedIn feed, then stop the login container (Ctrl+C in the "
        "terminal, or close it from the Containers page). The authenticated browser profile is "
        "saved to the `linkedin-session` volume.",
        "Run `make restart` so the long-running linkedin-mcp service picks the session up — the "
        "login container and linkedin-mcp cannot use the same Chromium profile at the same time.",
        "Come back here and re-check the connection (GET /api/linkedin/status?refresh=1). It "
        "should report authenticated.",
        "Reminder: even with a valid session, Hermes does not submit applications. The MCP server "
        "has no apply tool. Hermes ranks jobs, tailors your resume and gives you the apply link "
        "to click yourself.",
    ]
    instructions = "\n".join(f"{index}. {text}" for index, text in enumerate(steps, start=1))

    # The operator is about to change the session; make sure the next status call
    # re-probes instead of serving a stale "unauthenticated".
    invalidate_status_cache()
    try:
        get_mcp().invalidate_health_cache()
    except Exception:  # noqa: BLE001 - the URL may not even be configured yet
        log.debug("could not invalidate the MCP health cache", exc_info=True)

    log.info("handed out the manual LinkedIn login procedure (viewer %s)", viewer_url)
    return {
        "viewer_url": viewer_url,
        "instructions": instructions,
        "steps": steps,
        "viewer_port": settings.linkedin_viewer_port,
        "commands": {
            "make": "make login-linkedin",
            "windows": "powershell -ExecutionPolicy Bypass -File scripts\\linkedin-login.ps1",
            "posix": "bash scripts/linkedin-login.sh",
            "after_login": "make restart",
        },
        "can_auto_login": False,
        "automated": False,
        "requires_host_terminal": True,
        "mcp_url": settings.linkedin_mcp_url,
        "server_message_when_missing": NO_SESSION_MARKER,
    }
