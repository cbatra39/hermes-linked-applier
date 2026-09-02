"""``GET /api/health`` — one call that tells you which dependency is broken.

The endpoint is used by the container HEALTHCHECK and by the dashboard's
Overview page, so it must (a) always answer, (b) never hang. Each dependency
probe is wrapped in its own timeout and try/except: a dead freellmapi router
must not stop the response from reporting that LinkedIn MCP is fine.

``ok`` reflects *Hermes itself* (its database), not its dependencies — otherwise
the container would be restarted in a loop just because the user has not logged
into LinkedIn yet.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from fastapi import APIRouter
from sqlalchemy import text
from starlette.concurrency import run_in_threadpool

from hermes.db import SessionLocal
from hermes.llm import get_llm
from hermes.mcp_client import get_mcp
from hermes.routes._common import HERMES_VERSION
from hermes.sandbox import get_sandbox
from hermes.settings import settings

log = logging.getLogger("hermes.api.health")

router = APIRouter(tags=["health"])

#: Probes are short by design: the dashboard polls this endpoint.
_LLM_TIMEOUT = 8.0
_MCP_TIMEOUT = 10.0
_DOCKER_TIMEOUT = 8.0


def _db_probe() -> dict[str, Any]:
    try:
        with SessionLocal() as db:
            db.execute(text("SELECT 1"))
        return {"reachable": True, "detail": "ok"}
    except Exception as exc:
        log.error("database probe failed: %s", exc)
        return {"reachable": False, "detail": f"{type(exc).__name__}: {exc}"}


async def _llm_probe() -> dict[str, Any]:
    base_url = str(getattr(settings, "freellmapi_base_url", "") or "")
    info: dict[str, Any] = {
        "reachable": False,
        "base_url": base_url,
        "key_configured": bool(str(getattr(settings, "freellmapi_key", "") or "").strip()),
        "models": 0,
        "primary": None,
        "detail": "",
    }
    try:
        llm = get_llm()
    except Exception as exc:
        info["detail"] = f"router not configured: {exc}"
        return info

    info["primary"] = getattr(llm, "primary", None) or None
    info["fallbacks"] = list(getattr(llm, "fallbacks", []) or [])
    try:
        models = await asyncio.wait_for(llm.list_models(), timeout=_LLM_TIMEOUT)
        info["reachable"] = True
        info["models"] = len(models or [])
        info["detail"] = "ok"
        if not info["primary"] and models:
            first = models[0]
            info["primary"] = first.get("id") if isinstance(first, dict) else str(first)
            info["detail"] = "ok (model auto-picked)"
    except asyncio.TimeoutError:
        info["detail"] = f"timed out after {_LLM_TIMEOUT:.0f}s"
    except Exception as exc:
        info["detail"] = f"{type(exc).__name__}: {exc}"
    # `ok` is an alias of `reachable`. The MCP block uses `reachable`, so the
    # dashboard reaches for both spellings depending on the page; emitting both
    # keeps every consumer correct instead of silently rendering "LLM down".
    info["ok"] = info["reachable"]
    return info


async def _mcp_probe() -> dict[str, Any]:
    info: dict[str, Any] = {
        "reachable": False,
        "authenticated": False,
        "url": str(getattr(settings, "linkedin_mcp_url", "") or ""),
        "detail": "",
    }
    try:
        mcp = get_mcp()
        health = await asyncio.wait_for(mcp.health(), timeout=_MCP_TIMEOUT)
        if isinstance(health, dict):
            info.update(
                {
                    "reachable": bool(health.get("reachable")),
                    "authenticated": bool(health.get("authenticated")),
                    "detail": str(health.get("detail") or "ok"),
                }
            )
        else:  # pragma: no cover - contract says dict
            info["detail"] = f"unexpected health payload: {type(health).__name__}"
    except asyncio.TimeoutError:
        info["detail"] = f"timed out after {_MCP_TIMEOUT:.0f}s"
    except Exception as exc:
        info["detail"] = f"{type(exc).__name__}: {exc}"
    return info


def _docker_probe_sync() -> dict[str, Any]:
    try:
        sandbox = get_sandbox()
        containers = sandbox.list_containers()
        return {"reachable": True, "containers": len(containers or []), "detail": "ok"}
    except Exception as exc:
        return {"reachable": False, "containers": 0, "detail": f"{type(exc).__name__}: {exc}"}


async def _docker_probe() -> dict[str, Any]:
    try:
        return await asyncio.wait_for(run_in_threadpool(_docker_probe_sync), timeout=_DOCKER_TIMEOUT)
    except asyncio.TimeoutError:
        return {
            "reachable": False,
            "containers": 0,
            "detail": f"timed out after {_DOCKER_TIMEOUT:.0f}s",
        }


@router.get("/health", summary="Service + dependency health")
async def health() -> dict[str, Any]:
    """Aggregate health of Hermes and its three external dependencies."""
    db_info, llm_info, mcp_info, docker_info = await asyncio.gather(
        run_in_threadpool(_db_probe),
        _llm_probe(),
        _mcp_probe(),
        _docker_probe(),
    )
    return {
        # `ok` == "this service can serve requests", deliberately independent of
        # whether the user has configured an LLM key or logged into LinkedIn.
        "ok": bool(db_info["reachable"]),
        "version": HERMES_VERSION,
        "llm": llm_info,
        "mcp": mcp_info,
        # Contract says `docker` is a bool; the detail object is additive.
        "docker": bool(docker_info["reachable"]),
        "docker_detail": docker_info,
        "db": db_info,
    }
