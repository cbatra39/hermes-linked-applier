"""``GET /api/llm/models`` and ``POST /api/llm/test`` — the freellmapi router.

Both endpoints are thin proxies over :class:`hermes.llm.LLMRouter`, and both
exist for exactly one reason: when Hermes says "the LLM is broken", the operator
must be able to tell *which* of the three plausible causes it is —

* the key is not configured at all            -> ``503`` (nothing to reach)
* the router container is down / rejects us   -> ``502`` (upstream failure)
* the router is up but every model failed     -> ``502`` with the per-model reasons

so the status code alone narrows the problem, and ``detail`` says what to do
about it. That mapping lives in :func:`_http_error`; keep it there rather than
spreading ``try/except`` over the handlers.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

from fastapi import APIRouter, Body, HTTPException, Query

from hermes.llm import (
    LLMAllModelsFailed,
    LLMConfigError,
    LLMHTTPError,
    LLMError,
    LLMRouter,
    get_llm,
)
from hermes.settings import ConfigError, settings

log = logging.getLogger("hermes.api.llm")

#: No prefix + full paths in the decorators, matching routes/health.py.
router = APIRouter(tags=["llm"])

#: ``GET /v1/models`` is a cheap listing; the dashboard polls it from the
#: Settings page, so it must fail fast rather than hang the page.
_LIST_TIMEOUT_S = 25.0

#: A connectivity test, not a workload: free providers can still be slow to
#: first byte, but three minutes is already a failure from the UI's point of view.
_TEST_TIMEOUT_S = 150.0

#: Deliberately tiny — this proves the round trip, it does not benchmark quality.
DEFAULT_TEST_PROMPT = "Reply with exactly: hermes ok"
DEFAULT_TEST_MAX_TOKENS = 64
MAX_TEST_PROMPT_CHARS = 8000


# --------------------------------------------------------------------------- #
# error mapping
# --------------------------------------------------------------------------- #


def _http_error(exc: BaseException) -> HTTPException:
    """Translate an LLM-layer exception into the right status + fix-it text."""
    if isinstance(exc, asyncio.TimeoutError):
        return HTTPException(
            status_code=504,
            detail=(
                f"The freellmapi router at {settings.freellmapi_base_url} did not answer in time. "
                "Check `docker compose logs freellmapi` — a cold free provider can take a "
                "while, but a healthy router responds to GET /api/ping immediately."
            ),
        )
    if isinstance(exc, LLMConfigError):
        # Two very different causes share this class: nothing configured locally
        # (503 — we never left the building) vs. the router rejecting our token.
        if not (settings.freellmapi_key or "").strip():
            return HTTPException(status_code=503, detail=str(exc))
        return HTTPException(status_code=502, detail=str(exc))
    if isinstance(exc, LLMHTTPError):
        return HTTPException(
            status_code=502,
            detail=f"freellmapi returned HTTP {exc.status_code}: {exc}",
        )
    if isinstance(exc, LLMAllModelsFailed):
        return HTTPException(status_code=502, detail=str(exc))
    if isinstance(exc, (LLMError, ConfigError)):
        return HTTPException(status_code=502, detail=str(exc))
    return HTTPException(
        status_code=502,
        detail=f"Unexpected LLM failure: {type(exc).__name__}: {exc}",
    )


def _router() -> LLMRouter:
    """The shared router, or a 503 that says how to configure it."""
    try:
        settings.require_llm()
    except ConfigError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    try:
        return get_llm()
    except Exception as exc:  # noqa: BLE001 - mapped to a clean HTTP error
        raise _http_error(exc) from exc


# --------------------------------------------------------------------------- #
# routes
# --------------------------------------------------------------------------- #


@router.get("/llm/models", summary="Models the freellmapi router can serve")
async def list_models(
    refresh: bool = Query(False, description="Bypass the 5 minute model cache."),
) -> dict[str, Any]:
    """Proxy ``LLMRouter.list_models()`` plus the resolved failover chain."""
    llm = _router()
    try:
        models = await asyncio.wait_for(
            llm.list_models(force_refresh=refresh), timeout=_LIST_TIMEOUT_S
        )
        chain = await asyncio.wait_for(llm.resolve_chain(), timeout=_LIST_TIMEOUT_S)
    except Exception as exc:  # noqa: BLE001 - every branch becomes a clean 5xx
        log.warning("GET /llm/models failed: %s: %s", type(exc).__name__, exc)
        raise _http_error(exc) from exc

    primary = llm.primary or (chain[0] if chain else None)
    detail = f"{len(models)} model(s) available via {llm.base_url}"
    if not llm.primary and primary:
        detail += f"; no HERMES_MODEL_PRIMARY set, auto-picked {primary}"
    return {
        "models": models,
        "primary": primary,
        "fallbacks": list(chain[1:]) or list(llm.fallbacks),
        "chain": chain,
        "base_url": llm.base_url,
        "detail": detail,
    }


@router.post("/llm/test", summary="Round-trip one tiny prompt through the router")
async def test_llm(payload: Any = Body(default=None)) -> dict[str, Any]:
    """Send a minimal prompt and report which model answered, and how fast.

    Body (all optional): ``{"prompt", "model", "temperature", "max_tokens"}``.
    Returns ``{"model", "output", "latency_ms"}`` — plus the token usage and the
    chain that was tried, which is additive and useful when a free provider
    silently truncates.
    """
    body: dict[str, Any] = payload if isinstance(payload, dict) else {}

    prompt = str(body.get("prompt") or DEFAULT_TEST_PROMPT).strip() or DEFAULT_TEST_PROMPT
    if len(prompt) > MAX_TEST_PROMPT_CHARS:
        raise HTTPException(
            status_code=422,
            detail=(
                f"prompt is {len(prompt)} characters; the connectivity test accepts at "
                f"most {MAX_TEST_PROMPT_CHARS}. Use the pipeline for real work."
            ),
        )

    model = body.get("model")
    model = str(model).strip() if isinstance(model, str) and model.strip() else None

    try:
        temperature = float(body.get("temperature", 0.2))
    except (TypeError, ValueError):
        raise HTTPException(status_code=422, detail="temperature must be a number.") from None
    if not 0.0 <= temperature <= 2.0:
        raise HTTPException(status_code=422, detail="temperature must be between 0 and 2.")

    raw_max = body.get("max_tokens", DEFAULT_TEST_MAX_TOKENS)
    try:
        max_tokens = int(raw_max) if raw_max is not None else DEFAULT_TEST_MAX_TOKENS
    except (TypeError, ValueError):
        raise HTTPException(status_code=422, detail="max_tokens must be a whole number.") from None
    if not 1 <= max_tokens <= 4096:
        raise HTTPException(
            status_code=422,
            detail="max_tokens must be between 1 and 4096 for the connectivity test.",
        )

    llm = _router()
    started = time.perf_counter()
    try:
        result = await asyncio.wait_for(
            llm.chat_detailed(
                [{"role": "user", "content": prompt}],
                model=model,
                temperature=temperature,
                max_tokens=max_tokens,
            ),
            timeout=_TEST_TIMEOUT_S,
        )
    except Exception as exc:  # noqa: BLE001 - every branch becomes a clean 5xx
        log.warning("POST /llm/test failed: %s: %s", type(exc).__name__, exc)
        raise _http_error(exc) from exc

    latency_ms = result.latency_ms or int((time.perf_counter() - started) * 1000)
    log.info("llm test ok: model=%s %dms", result.model, latency_ms)
    return {
        "model": result.model,
        "output": result.output,
        "latency_ms": latency_ms,
        "usage": result.usage,
        "attempts": result.attempts,
        "finish_reason": result.finish_reason,
        "prompt": prompt,
    }
