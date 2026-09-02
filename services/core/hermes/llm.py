"""
hermes.llm — OpenAI-compatible LLM client for the self-hosted `freellmapi` router.

`freellmapi` (ghcr.io/tashfeenahmed/freellmapi) aggregates the free tiers of many
providers behind one OpenAI-compatible surface:

    POST {base}/v1/chat/completions
    GET  {base}/v1/models
    GET  {base}/api/ping            (health, NOT under /v1)

Because every upstream is a *free* tier, transient failure is the normal case, not
the exception: 429 rate limits, 5xx from an exhausted provider, cold-start
timeouts, models that vanish from /v1/models between calls, and providers that
reject perfectly legal OpenAI fields (`response_format`, `max_tokens`,
`temperature`). `LLMRouter` therefore treats "which model serves this call" as a
runtime decision:

  * models are discovered from /v1/models and cached (TTL) so a blank
    HERMES_MODEL_PRIMARY still works — a chat-capable model is auto-picked;
  * a failover chain (primary -> fallbacks -> auto-picked) is walked in order;
  * each model gets bounded retries with exponential backoff + jitter, honouring
    `Retry-After` (seconds or HTTP-date);
  * a 400 that names an unsupported field triggers a payload adaptation and one
    more attempt instead of burning the whole model;
  * the model that actually served the call is logged and emitted to the run's
    event stream, so the dashboard shows reality rather than configuration.

Nothing here streams: Hermes needs whole documents (resume markdown, JSON
analyses), and streaming would only complicate failover.
"""

from __future__ import annotations

import asyncio
import inspect
import json
import logging
import os
import random
import re
import time
from dataclasses import dataclass, field
from email.utils import parsedate_to_datetime
from datetime import datetime, timezone
from typing import Any, Iterable, Sequence

import httpx

from hermes.settings import settings

try:  # pragma: no cover - the foundation module always exists in the container
    from hermes.events import bus
except Exception:  # pragma: no cover - keeps llm.py importable in isolation/tests
    bus = None  # type: ignore[assignment]

logger = logging.getLogger("hermes.llm")

__all__ = [
    "LLMError",
    "LLMConfigError",
    "LLMHTTPError",
    "LLMAllModelsFailed",
    "LLMResult",
    "LLMRouter",
    "get_llm",
    "reset_llm",
    "extract_json",
]

# ---------------------------------------------------------------------------
# Tunables
# ---------------------------------------------------------------------------

#: How long a discovered /v1/models listing stays fresh.
MODEL_CACHE_TTL_S = 300.0

#: Attempts per model before moving to the next one in the chain.
MAX_ATTEMPTS_PER_MODEL = 3

#: Backoff schedule (seconds) for retryable failures; jitter is added.
BACKOFF_BASE_S = 1.5
BACKOFF_MAX_S = 30.0

#: Never sleep longer than this even if a provider sends a huge Retry-After.
RETRY_AFTER_CAP_S = 60.0

#: HTTP timeouts. Free tiers routinely take 30-90s to first byte on a cold
#: provider, so the read timeout is deliberately generous.
CONNECT_TIMEOUT_S = 15.0
READ_TIMEOUT_S = 180.0
WRITE_TIMEOUT_S = 30.0
POOL_TIMEOUT_S = 15.0

#: Substring preferences used when no model is configured. Earlier == better.
#: Biased towards large-context, fast, reliably-free instruct models.
MODEL_PREFERENCE: tuple[str, ...] = (
    "gemini-2.5-flash",
    "gemini-2.0-flash",
    "gemini-1.5-flash",
    "gemini",
    "llama-3.3-70b",
    "llama-3.1-70b",
    "llama-4",
    "llama-3",
    "deepseek-v3",
    "deepseek-chat",
    "qwen2.5-72b",
    "qwen",
    "gpt-oss",
    "mistral-large",
    "mistral",
    "gemma-3",
    "gemma",
    "command-r",
    "glm-4",
    "kimi",
    "phi-4",
)

#: Models that cannot serve chat completions, filtered out of auto-pick.
NON_CHAT_MARKERS: tuple[str, ...] = (
    "embed", "embedding", "bge-", "gte-", "e5-", "rerank", "moderation",
    "whisper", "tts", "speech", "audio", "voice", "transcribe",
    "dall-e", "imagen", "stable-diffusion", "sdxl", "flux", "veo", "kling",
    "image-generation", "-image", "vision-encoder", "clip-", "guard",
)

#: Reasoning models (deepseek-r1, qwen-thinking, ...) leak chain-of-thought in
#: the content field on several free providers. Strip it — Hermes wants answers.
_THINK_RE = re.compile(r"<(think|thinking|reasoning)>.*?</\1>", re.DOTALL | re.IGNORECASE)
_UNCLOSED_THINK_RE = re.compile(r"^\s*<(think|thinking|reasoning)>.*?(?=\n\s*\S)", re.DOTALL | re.IGNORECASE)


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------

class LLMError(RuntimeError):
    """Base class for every failure raised by this module."""


class LLMConfigError(LLMError):
    """Configuration is missing or nonsensical (no API key, no base URL)."""


class LLMHTTPError(LLMError):
    """A non-retryable HTTP error from the router."""

    def __init__(self, status_code: int, message: str, *, model: str | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.model = model


class LLMAllModelsFailed(LLMError):
    """Every model in the failover chain failed. Carries the per-model reasons."""

    def __init__(self, attempts: dict[str, str]) -> None:
        detail = "; ".join(f"{m}: {e}" for m, e in attempts.items()) or "no models available"
        super().__init__(
            "All LLM models failed. Tried -> " + detail + ". "
            "Check the freellmapi dashboard (provider keys / quota) and HERMES_MODEL_* in .env."
        )
        self.attempts = attempts


# ---------------------------------------------------------------------------
# Result envelope
# ---------------------------------------------------------------------------

@dataclass(slots=True)
class LLMResult:
    """
    Full outcome of one chat call.

    `LLMRouter.chat()` returns only `output` (per the build contract). Callers that
    need to report *which* model served a call — e.g. `POST /api/llm/test`, which
    must return {"model","output","latency_ms"} — should use `chat_detailed()`
    instead of reading `router.last_model`, which is racy under concurrency.
    """

    output: str
    model: str
    latency_ms: int
    usage: dict[str, Any] = field(default_factory=dict)
    attempts: int = 1
    finish_reason: str | None = None
    raw: dict[str, Any] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------

def _setting(name: str, default: Any = None) -> Any:
    """
    Read a Hermes setting, tolerating a settings module that has not yet grown
    the field (parallel development) by falling back to the raw environment.
    """
    value = getattr(settings, name, None)
    if value in (None, ""):
        value = os.getenv(name.upper())
    return default if value in (None, "") else value


def _as_list(value: Any) -> list[str]:
    """Normalise a comma-separated string or an iterable into a clean str list."""
    if value is None:
        return []
    if isinstance(value, str):
        parts: Iterable[str] = value.split(",")
    elif isinstance(value, (list, tuple, set)):
        parts = [str(v) for v in value]
    else:
        parts = [str(value)]
    out: list[str] = []
    for p in parts:
        p = p.strip()
        if p and p not in out:
            out.append(p)
    return out


def _redact(text: str, secret: str | None) -> str:
    """Never let the bearer token reach a log line, an event, or an exception."""
    if not text:
        return ""
    if secret and len(secret) > 8:
        text = text.replace(secret, secret[:12] + "...redacted")
    return re.sub(r"(freellmapi-)[A-Za-z0-9_\-]{6,}", r"\1...redacted", text)


def _truncate(text: str, limit: int = 400) -> str:
    text = " ".join((text or "").split())
    return text if len(text) <= limit else text[:limit] + "…"


def _parse_retry_after(value: str | None) -> float | None:
    """`Retry-After` is either delta-seconds or an HTTP-date. Support both."""
    if not value:
        return None
    value = value.strip()
    try:
        return max(0.0, float(value))
    except ValueError:
        pass
    try:
        when = parsedate_to_datetime(value)
        if when is None:
            return None
        if when.tzinfo is None:
            when = when.replace(tzinfo=timezone.utc)
        return max(0.0, (when - datetime.now(timezone.utc)).total_seconds())
    except (TypeError, ValueError, IndexError):
        return None


def _backoff_delay(attempt: int) -> float:
    """Exponential backoff with full jitter; `attempt` is 1-based."""
    ceiling = min(BACKOFF_MAX_S, BACKOFF_BASE_S * (2 ** max(0, attempt - 1)))
    return round(random.uniform(ceiling / 2.0, ceiling), 2)


def _strip_reasoning(text: str) -> str:
    """Remove <think>…</think> blocks emitted by reasoning models."""
    if "<" not in text:
        return text
    cleaned = _THINK_RE.sub("", text)
    if "</think" not in cleaned.lower():
        cleaned = _UNCLOSED_THINK_RE.sub("", cleaned)
    return cleaned.strip() or text.strip()


# ---------------------------------------------------------------------------
# JSON extraction — the reason chat_json() can be trusted
# ---------------------------------------------------------------------------

_FENCE_RE = re.compile(r"```[a-zA-Z0-9_+-]*\s*\n?(.*?)```", re.DOTALL)
_TRAILING_COMMA_RE = re.compile(r",(\s*[}\]])")


def _fence_bodies(text: str) -> list[str]:
    """Bodies of every ``` fenced block, longest first (most likely the payload)."""
    bodies = [m.group(1).strip() for m in _FENCE_RE.finditer(text)]
    # An unterminated fence is common when max_tokens truncates the reply.
    if not bodies and "```" in text:
        tail = text.split("```", 1)[1]
        tail = tail.split("\n", 1)[1] if "\n" in tail else tail
        bodies = [tail.strip()]
    return sorted(bodies, key=len, reverse=True)


def _balanced_spans(text: str, opener: str, closer: str) -> list[str]:
    """
    Every top-level balanced `opener…closer` span in `text`, outermost first.

    String literals and backslash escapes are respected, so braces inside JSON
    string values do not corrupt the depth count.
    """
    spans: list[str] = []
    depth = 0
    start = -1
    in_string = False
    escaped = False
    for i, ch in enumerate(text):
        if in_string:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                in_string = False
            continue
        if ch == '"':
            in_string = True
        elif ch == opener:
            if depth == 0:
                start = i
            depth += 1
        elif ch == closer and depth:
            depth -= 1
            if depth == 0 and start >= 0:
                spans.append(text[start : i + 1])
                start = -1
    if depth and start >= 0:
        # Truncated response: keep the fragment so the repair pass can see it.
        spans.append(text[start:])
    return spans


def _loads_relaxed(candidate: str) -> Any:
    """json.loads, retried once with trailing commas removed."""
    try:
        return json.loads(candidate)
    except json.JSONDecodeError:
        repaired = _TRAILING_COMMA_RE.sub(r"\1", candidate)
        if repaired != candidate:
            return json.loads(repaired)
        raise


def extract_json(text: str) -> Any:
    """
    Pull the first plausible JSON value out of a model reply.

    Handles, in order: clean JSON, ```json fenced JSON, prose-wrapped JSON,
    objects/arrays preceded by a preamble ("Here is the JSON:"), reasoning-model
    <think> blocks, and trailing commas. Raises `json.JSONDecodeError` if nothing
    parses, which is what triggers `chat_json`'s single repair retry.
    """
    if not text or not text.strip():
        raise json.JSONDecodeError("empty model response", text or "", 0)

    cleaned = _strip_reasoning(text).strip()
    candidates: list[str] = [cleaned]
    candidates.extend(_fence_bodies(cleaned))
    for body in [cleaned, *_fence_bodies(cleaned)]:
        candidates.extend(_balanced_spans(body, "{", "}"))
        candidates.extend(_balanced_spans(body, "[", "]"))

    seen: set[str] = set()
    last_error: json.JSONDecodeError | None = None
    for candidate in candidates:
        candidate = candidate.strip()
        if not candidate or candidate in seen:
            continue
        seen.add(candidate)
        if candidate[0] not in "{[":
            continue
        try:
            return _loads_relaxed(candidate)
        except json.JSONDecodeError as exc:
            last_error = exc

    raise last_error or json.JSONDecodeError(
        "no JSON object or array found in model response", cleaned, 0
    )


# ---------------------------------------------------------------------------
# Router
# ---------------------------------------------------------------------------

class LLMRouter:
    """
    Failover-aware client for an OpenAI-compatible endpoint (freellmapi).

    Thread/task-safe for concurrent `chat()` calls: the only mutable shared state
    is the model cache (guarded by a lock) and the `last_*` diagnostic fields
    (best-effort only — use `chat_detailed()` when the value matters).
    """

    def __init__(
        self,
        base_url: str | None = None,
        api_key: str | None = None,
        primary: str | None = None,
        fallbacks: Sequence[str] | str | None = None,
        *,
        timeout: float | None = None,
    ) -> None:
        base_url = str(base_url or _setting("freellmapi_base_url") or "").strip()
        if not base_url:
            raise LLMConfigError(
                "FREELLMAPI_BASE_URL is not set. Expected http://freellmapi:3001/v1 "
                "(service name inside the `hermes` compose network)."
            )
        self.base_url = self._normalise_base_url(base_url)
        self.root_url = self.base_url[: -len("/v1")] if self.base_url.endswith("/v1") else self.base_url

        self.api_key = (api_key if api_key is not None else _setting("freellmapi_key") or "")
        self.api_key = str(self.api_key).strip()

        self.primary = (primary if primary is not None else _setting("hermes_model_primary") or "")
        self.primary = str(self.primary).strip()
        self.fallbacks = _as_list(
            fallbacks if fallbacks is not None else _setting("hermes_model_fallbacks")
        )
        self.read_timeout = float(timeout or READ_TIMEOUT_S)

        self._client: httpx.AsyncClient | None = None
        self._client_loop: asyncio.AbstractEventLoop | None = None
        self._client_lock = asyncio.Lock()

        self._models_cache: list[dict[str, Any]] = []
        self._models_cached_at: float = 0.0
        self._models_lock = asyncio.Lock()
        self._auto_model: str | None = None

        # Best-effort diagnostics for logs/health; not authoritative.
        self.last_model: str | None = None
        self.last_latency_ms: int | None = None

        logger.info(
            "LLMRouter ready base=%s primary=%s fallbacks=%s key=%s",
            self.base_url,
            self.primary or "<auto>",
            ",".join(self.fallbacks) or "<none>",
            "present" if self.api_key else "MISSING",
        )

    # -- plumbing ----------------------------------------------------------

    @staticmethod
    def _normalise_base_url(url: str) -> str:
        """Accept `http://host:3001`, `.../v1` or `.../v1/`; always yield `.../v1`."""
        url = url.rstrip("/")
        if not url.endswith("/v1") and "/v1/" not in url + "/":
            url = url + "/v1"
        return url

    def _require_key(self) -> None:
        if not self.api_key:
            raise LLMConfigError(
                "FREELLMAPI_KEY is empty. Open the freellmapi dashboard on "
                f"{self.root_url} , add at least one free provider, mint a client token "
                "(it looks like `freellmapi-...`), then set FREELLMAPI_KEY in the root .env "
                "and restart hermes-core."
            )
        if not self.api_key.startswith("freellmapi-"):
            # Not fatal — a user may front the router with something else — but loud.
            logger.warning(
                "FREELLMAPI_KEY does not start with 'freellmapi-'; if calls 401, re-mint "
                "the client token in the freellmapi dashboard."
            )

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
            "Accept": "application/json",
            "User-Agent": "hermes-core/1.0 (+llm-router)",
        }

    async def _get_client(self) -> httpx.AsyncClient:
        """
        Lazily build the shared AsyncClient, rebuilding it if the event loop
        changed (test runners, `asyncio.run` per task) or it was closed.
        """
        loop = asyncio.get_running_loop()
        client = self._client
        if client is not None and not client.is_closed and self._client_loop is loop:
            return client
        async with self._client_lock:
            client = self._client
            if client is not None and not client.is_closed and self._client_loop is loop:
                return client
            if client is not None and not client.is_closed:
                try:
                    await client.aclose()
                except Exception:  # pragma: no cover - loop already gone
                    pass
            self._client = httpx.AsyncClient(
                timeout=httpx.Timeout(
                    connect=CONNECT_TIMEOUT_S,
                    read=self.read_timeout,
                    write=WRITE_TIMEOUT_S,
                    pool=POOL_TIMEOUT_S,
                ),
                limits=httpx.Limits(max_connections=16, max_keepalive_connections=8),
                follow_redirects=True,
                trust_env=False,  # ignore host proxy vars: this is an in-network hop
            )
            self._client_loop = loop
            return self._client

    async def aclose(self) -> None:
        """Close the underlying HTTP client. Safe to call repeatedly."""
        client, self._client = self._client, None
        self._client_loop = None
        if client is not None and not client.is_closed:
            await client.aclose()

    async def __aenter__(self) -> "LLMRouter":
        return self

    async def __aexit__(self, *_exc: Any) -> None:
        await self.aclose()

    async def _emit(self, run_id: str | None, message: str, level: str = "info") -> None:
        """Mirror progress to the run's SSE stream. Never fails a call."""
        logger.log(logging.WARNING if level in ("warn", "warning", "error") else logging.INFO, message)
        if not run_id or bus is None:
            return
        try:
            result = bus.publish(run_id, level, message)
            if inspect.isawaitable(result):
                await result
        except Exception:  # pragma: no cover - observability must never break work
            logger.debug("event publish failed for run=%s", run_id, exc_info=True)

    # -- model discovery ---------------------------------------------------

    async def list_models(self, *, force_refresh: bool = False) -> list[dict[str, Any]]:
        """
        GET /v1/models, normalised to a list of dicts each having at least "id".

        Cached for MODEL_CACHE_TTL_S because the dashboard's Settings page and
        every auto-pick would otherwise hammer the router.
        """
        self._require_key()
        now = time.monotonic()
        if not force_refresh and self._models_cache and (now - self._models_cached_at) < MODEL_CACHE_TTL_S:
            return list(self._models_cache)

        async with self._models_lock:
            now = time.monotonic()
            if not force_refresh and self._models_cache and (now - self._models_cached_at) < MODEL_CACHE_TTL_S:
                return list(self._models_cache)

            client = await self._get_client()
            url = f"{self.base_url}/models"
            try:
                response = await client.get(url, headers=self._headers())
            except httpx.HTTPError as exc:
                raise LLMError(
                    f"Cannot reach the freellmapi router at {url}: "
                    f"{_redact(str(exc), self.api_key) or type(exc).__name__}. "
                    "Is the `freellmapi` container healthy (GET /api/ping)?"
                ) from exc

            if response.status_code in (401, 403):
                raise LLMConfigError(
                    f"freellmapi rejected the client token ({response.status_code}). "
                    "Re-mint a `freellmapi-...` token in its dashboard and update FREELLMAPI_KEY."
                )
            if response.status_code >= 400:
                raise LLMHTTPError(
                    response.status_code,
                    f"GET {url} failed: {response.status_code} "
                    f"{_truncate(_redact(response.text, self.api_key), 200)}",
                )

            payload = response.json()
            raw = payload.get("data") if isinstance(payload, dict) else payload
            models: list[dict[str, Any]] = []
            for item in raw or []:
                if isinstance(item, str):
                    models.append({"id": item})
                elif isinstance(item, dict) and item.get("id"):
                    models.append(item)
            self._models_cache = models
            self._models_cached_at = time.monotonic()
            self._auto_model = None  # re-evaluate against the fresh listing
            logger.info("discovered %d model(s) from %s", len(models), url)
            return list(models)

    @staticmethod
    def _is_chat_capable(model_id: str) -> bool:
        low = model_id.lower()
        return not any(marker in low for marker in NON_CHAT_MARKERS)

    @classmethod
    def _preference_rank(cls, model_id: str) -> tuple[int, int, str]:
        low = model_id.lower()
        for index, needle in enumerate(MODEL_PREFERENCE):
            if needle in low:
                return (index, len(model_id), low)
        return (len(MODEL_PREFERENCE), len(model_id), low)

    async def pick_model(self, *, force_refresh: bool = False) -> str:
        """
        Choose a chat model when none is configured, preferring known-good free
        instruct models (see MODEL_PREFERENCE) and skipping embedding/audio/image
        models. Result is memoised until the model cache refreshes.
        """
        if self._auto_model and not force_refresh:
            return self._auto_model
        models = await self.list_models(force_refresh=force_refresh)
        candidates = [m["id"] for m in models if self._is_chat_capable(str(m.get("id", "")))]
        if not candidates:
            raise LLMConfigError(
                "freellmapi exposed no chat-capable models. Add at least one free provider "
                "in its dashboard, or set HERMES_MODEL_PRIMARY to a model it does serve."
            )
        candidates.sort(key=self._preference_rank)
        self._auto_model = candidates[0]
        logger.info("auto-selected model %s (of %d candidates)", self._auto_model, len(candidates))
        return self._auto_model

    async def resolve_chain(self, model: str | None = None) -> list[str]:
        """
        Ordered list of models to try for one logical call.

        Explicit `model` first (a deliberate caller choice), then the configured
        primary, then the configured fallbacks, then — only if that leaves us with
        nothing — an auto-picked model from /v1/models.
        """
        chain: list[str] = []
        for candidate in [model, self.primary, *self.fallbacks]:
            candidate = (candidate or "").strip()
            if candidate and candidate not in chain:
                chain.append(candidate)
        if not chain:
            chain.append(await self.pick_model())
        return chain

    # -- payload adaptation for strict/limited upstreams --------------------

    @staticmethod
    def _adapt_payload(payload: dict[str, Any], error_text: str) -> tuple[dict[str, Any] | None, str]:
        """
        Some free upstreams 400 on legal OpenAI fields. Rather than discarding the
        model, drop/rename the offending field once and retry.

        Returns (new_payload, human_description) or (None, "") if nothing applies.
        """
        low = (error_text or "").lower()
        adapted = dict(payload)

        if "response_format" in adapted and (
            "response_format" in low or "json_object" in low or "json mode" in low
        ):
            adapted.pop("response_format", None)
            return adapted, "dropped response_format (upstream lacks JSON mode)"

        if "max_tokens" in adapted and "max_completion_tokens" in low:
            adapted["max_completion_tokens"] = adapted.pop("max_tokens")
            return adapted, "renamed max_tokens -> max_completion_tokens"

        if "max_tokens" in adapted and "max_tokens" in low:
            adapted.pop("max_tokens", None)
            return adapted, "dropped max_tokens (rejected by upstream)"

        if "temperature" in adapted and "temperature" in low:
            adapted.pop("temperature", None)
            return adapted, "dropped temperature (fixed by upstream)"

        if "response_format" in adapted and "unsupported" in low:
            adapted.pop("response_format", None)
            return adapted, "dropped response_format (reported unsupported)"

        return None, ""

    @staticmethod
    def _extract_content(data: dict[str, Any]) -> tuple[str, str | None]:
        """
        Pull assistant text out of a completion, tolerating the shapes free
        providers actually return: standard `message.content`, content given as a
        list of blocks, reasoning-only replies, and legacy `choices[].text`.
        """
        choices = data.get("choices") or []
        if not choices:
            return "", None
        choice = choices[0] if isinstance(choices[0], dict) else {}
        finish_reason = choice.get("finish_reason") or choice.get("finishReason")
        message = choice.get("message") or {}

        content: Any = message.get("content")
        if isinstance(content, list):  # multimodal block list
            parts: list[str] = []
            for block in content:
                if isinstance(block, dict):
                    parts.append(str(block.get("text") or block.get("content") or ""))
                elif isinstance(block, str):
                    parts.append(block)
            content = "".join(parts)

        text = (content or "").strip() if isinstance(content, str) else ""
        if not text:
            text = str(message.get("reasoning_content") or message.get("reasoning") or "").strip()
        if not text:
            text = str(choice.get("text") or "").strip()
        return _strip_reasoning(text), finish_reason

    # -- the core call -----------------------------------------------------

    async def chat_detailed(
        self,
        messages: list[dict[str, Any]],
        *,
        model: str | None = None,
        temperature: float = 0.2,
        max_tokens: int | None = None,
        json_mode: bool = False,
        run_id: str | None = None,
        extra_body: dict[str, Any] | None = None,
    ) -> LLMResult:
        """
        Run one chat completion with model failover, returning the full envelope
        (output + which model served it + latency + token usage).

        Raises `LLMConfigError` for missing/invalid config, `LLMHTTPError` for a
        non-retryable error on the only viable model, and `LLMAllModelsFailed`
        when the whole chain is exhausted.
        """
        self._require_key()
        if not messages:
            raise ValueError("chat() requires at least one message")

        chain = await self.resolve_chain(model)
        client = await self._get_client()
        url = f"{self.base_url}/chat/completions"
        attempts_log: dict[str, str] = {}
        total_attempts = 0
        started_all = time.perf_counter()

        for model_index, model_id in enumerate(chain):
            payload: dict[str, Any] = {
                "model": model_id,
                "messages": messages,
                "temperature": float(temperature),
                "stream": False,
            }
            if max_tokens:
                payload["max_tokens"] = int(max_tokens)
            if json_mode:
                payload["response_format"] = {"type": "json_object"}
            if extra_body:
                payload.update(extra_body)

            adaptations_left = 3
            attempt = 0
            while attempt < MAX_ATTEMPTS_PER_MODEL:
                attempt += 1
                total_attempts += 1
                started = time.perf_counter()
                try:
                    response = await client.post(url, headers=self._headers(), json=payload)
                except (httpx.TimeoutException, httpx.TransportError) as exc:
                    reason = f"{type(exc).__name__}: {_truncate(_redact(str(exc), self.api_key), 160)}"
                    attempts_log[model_id] = reason
                    if attempt >= MAX_ATTEMPTS_PER_MODEL:
                        await self._emit(run_id, f"LLM {model_id} unreachable ({reason})", "warn")
                        break
                    delay = _backoff_delay(attempt)
                    await self._emit(
                        run_id, f"LLM {model_id} {reason} — retry {attempt}/{MAX_ATTEMPTS_PER_MODEL} in {delay}s", "warn"
                    )
                    await asyncio.sleep(delay)
                    continue

                latency_ms = int((time.perf_counter() - started) * 1000)
                status = response.status_code

                if status in (401, 403):
                    raise LLMConfigError(
                        f"freellmapi rejected the client token ({status}) on {url}. "
                        "Re-mint a `freellmapi-...` token in its dashboard and update FREELLMAPI_KEY."
                    )

                if status == 429 or status >= 500:
                    body = _truncate(_redact(response.text, self.api_key), 200)
                    reason = f"HTTP {status} {body}"
                    attempts_log[model_id] = reason
                    if attempt >= MAX_ATTEMPTS_PER_MODEL:
                        await self._emit(run_id, f"LLM {model_id} exhausted: {reason}", "warn")
                        break
                    retry_after = _parse_retry_after(response.headers.get("Retry-After"))
                    delay = min(retry_after, RETRY_AFTER_CAP_S) if retry_after is not None else _backoff_delay(attempt)
                    label = "rate-limited" if status == 429 else "upstream error"
                    await self._emit(
                        run_id,
                        f"LLM {model_id} {label} ({status}) — retry {attempt}/{MAX_ATTEMPTS_PER_MODEL} in {delay:.1f}s",
                        "warn",
                    )
                    await asyncio.sleep(delay)
                    continue

                if status >= 400:
                    body = _truncate(_redact(response.text, self.api_key), 300)
                    adapted, description = (
                        self._adapt_payload(payload, response.text) if adaptations_left > 0 else (None, "")
                    )
                    if adapted is not None:
                        adaptations_left -= 1
                        payload = adapted
                        attempt -= 1  # an adaptation is not a failed attempt
                        await self._emit(run_id, f"LLM {model_id}: {description}; retrying", "warn")
                        continue
                    reason = f"HTTP {status} {body}"
                    attempts_log[model_id] = reason
                    await self._emit(run_id, f"LLM {model_id} rejected the request: {reason}", "warn")
                    if len(chain) == 1:
                        raise LLMHTTPError(status, f"POST {url} failed: {reason}", model=model_id)
                    break  # try the next model

                # 2xx
                try:
                    data = response.json()
                except ValueError as exc:
                    reason = f"non-JSON response: {_truncate(_redact(response.text, self.api_key), 160)}"
                    attempts_log[model_id] = reason
                    if attempt >= MAX_ATTEMPTS_PER_MODEL:
                        break
                    await asyncio.sleep(_backoff_delay(attempt))
                    continue

                # Some routers report upstream failure inside a 200 body.
                if isinstance(data, dict) and data.get("error") and not data.get("choices"):
                    err = data["error"]
                    reason = _truncate(
                        _redact(err.get("message") if isinstance(err, dict) else str(err), self.api_key), 200
                    )
                    attempts_log[model_id] = f"router error: {reason}"
                    if attempt >= MAX_ATTEMPTS_PER_MODEL:
                        break
                    await self._emit(run_id, f"LLM {model_id} router error: {reason} — retrying", "warn")
                    await asyncio.sleep(_backoff_delay(attempt))
                    continue

                text, finish_reason = self._extract_content(data if isinstance(data, dict) else {})
                if not text:
                    reason = f"empty completion (finish_reason={finish_reason})"
                    attempts_log[model_id] = reason
                    if attempt >= MAX_ATTEMPTS_PER_MODEL:
                        break
                    await self._emit(run_id, f"LLM {model_id} returned nothing — retrying", "warn")
                    await asyncio.sleep(_backoff_delay(attempt))
                    continue

                served_by = str(data.get("model") or model_id) if isinstance(data, dict) else model_id
                usage = data.get("usage") if isinstance(data, dict) else None
                usage = usage if isinstance(usage, dict) else {}
                self.last_model = served_by
                self.last_latency_ms = latency_ms
                note = "" if model_index == 0 else f" (failover #{model_index})"
                await self._emit(
                    run_id,
                    f"LLM served by {served_by}{note} in {latency_ms} ms"
                    + (f", {usage.get('total_tokens')} tokens" if usage.get("total_tokens") else "")
                    + (f", finish={finish_reason}" if finish_reason and finish_reason != "stop" else ""),
                )
                if finish_reason == "length":
                    await self._emit(
                        run_id,
                        f"LLM {served_by} hit the token limit — output is truncated; raise max_tokens.",
                        "warn",
                    )
                return LLMResult(
                    output=text,
                    model=served_by,
                    latency_ms=latency_ms,
                    usage=usage,
                    attempts=total_attempts,
                    finish_reason=finish_reason,
                    raw=data if isinstance(data, dict) else {},
                )

            if model_index + 1 < len(chain):
                await self._emit(
                    run_id, f"LLM failing over from {model_id} to {chain[model_index + 1]}", "warn"
                )

        elapsed = int((time.perf_counter() - started_all) * 1000)
        await self._emit(run_id, f"LLM chain exhausted after {total_attempts} attempt(s) / {elapsed} ms", "error")
        raise LLMAllModelsFailed(attempts_log)

    async def chat(
        self,
        messages: list[dict[str, Any]],
        *,
        model: str | None = None,
        temperature: float = 0.2,
        max_tokens: int | None = None,
        json_mode: bool = False,
        run_id: str | None = None,
    ) -> str:
        """Contract entrypoint: run a chat completion, return the text content."""
        result = await self.chat_detailed(
            messages,
            model=model,
            temperature=temperature,
            max_tokens=max_tokens,
            json_mode=json_mode,
            run_id=run_id,
        )
        return result.output

    async def chat_json(
        self,
        messages: list[dict[str, Any]],
        *,
        schema_hint: str,
        **kw: Any,
    ) -> dict[str, Any]:
        """
        Chat and return parsed JSON.

        Robustness, in order of application:
          1. `schema_hint` is appended as a final system instruction and JSON mode
             is requested (unless the caller passes json_mode=False).
          2. The reply is run through `extract_json`: fence stripping, <think>
             removal, outermost balanced-brace matching, trailing-comma repair.
          3. On JSONDecodeError, ONE repair round-trip: the bad output plus the
             parser error are handed back with an instruction to emit only JSON,
             at temperature 0.

        A top-level JSON array is wrapped as {"items": [...]} so the return type
        is always a dict, as the contract requires.
        """
        kw.setdefault("json_mode", True)
        run_id = kw.get("run_id")
        instruction = (
            "Respond with a single valid JSON value and nothing else. "
            "No markdown, no code fences, no commentary, no trailing commas. "
            "Required shape:\n" + schema_hint.strip()
        )
        primed: list[dict[str, Any]] = [*messages, {"role": "system", "content": instruction}]

        result = await self.chat_detailed(primed, **kw)
        try:
            return self._as_dict(extract_json(result.output))
        except json.JSONDecodeError as exc:
            await self._emit(
                run_id,
                f"LLM JSON parse failed ({exc.msg}) — issuing one repair request to {result.model}",
                "warn",
            )

        repair_messages: list[dict[str, Any]] = [
            *primed,
            {"role": "assistant", "content": _truncate(result.output, 6000)},
            {
                "role": "user",
                "content": (
                    "That response could not be parsed as JSON. Output ONLY the corrected JSON "
                    "value — start with '{' or '[', end with '}' or ']', no prose, no code fence. "
                    "Required shape:\n" + schema_hint.strip()
                ),
            },
        ]
        repair_kw = dict(kw)
        repair_kw["temperature"] = 0.0
        # Pin the repair to the model that just answered: it is provably reachable.
        repair_kw.setdefault("model", result.model)
        repaired = await self.chat_detailed(repair_messages, **repair_kw)
        try:
            return self._as_dict(extract_json(repaired.output))
        except json.JSONDecodeError as exc:
            raise LLMError(
                f"Model {repaired.model} did not return valid JSON after a repair retry "
                f"({exc.msg}). First 300 chars: {_truncate(repaired.output, 300)}"
            ) from exc

    @staticmethod
    def _as_dict(value: Any) -> dict[str, Any]:
        """Normalise any parsed JSON value into a dict."""
        if isinstance(value, dict):
            return value
        if isinstance(value, list):
            return {"items": value}
        return {"value": value}

    # -- health ------------------------------------------------------------

    async def health(self) -> dict[str, Any]:
        """
        Cheap, never-raising status probe for `GET /api/health`.

        Returns {"ok", "reachable", "key_present", "models", "model_ids",
                 "chain", "detail", "base_url", "ping_ms"}.
        """
        info: dict[str, Any] = {
            "ok": False,
            "reachable": False,
            "key_present": bool(self.api_key),
            "models": 0,
            "model_ids": [],
            "chain": [],
            "detail": "",
            "base_url": self.base_url,
            "ping_ms": None,
        }
        # 1) Liveness via the router's own health endpoint (no auth, not under /v1).
        try:
            client = await self._get_client()
            started = time.perf_counter()
            ping = await client.get(f"{self.root_url}/api/ping", timeout=10.0)
            info["ping_ms"] = int((time.perf_counter() - started) * 1000)
            info["reachable"] = ping.status_code < 500
        except Exception as exc:
            info["detail"] = (
                f"freellmapi unreachable at {self.root_url}: "
                f"{_truncate(_redact(str(exc), self.api_key), 160) or type(exc).__name__}"
            )
            return info

        if not self.api_key:
            info["detail"] = "Router is up but FREELLMAPI_KEY is empty — mint a client token in its dashboard."
            return info

        # 2) Auth + model availability.
        try:
            models = await self.list_models()
            info["models"] = len(models)
            info["model_ids"] = [str(m.get("id")) for m in models][:100]
            info["chain"] = await self.resolve_chain()
            info["ok"] = bool(info["chain"])
            info["detail"] = (
                f"{len(models)} model(s) available; chain: {', '.join(info['chain'])}"
                if info["ok"]
                else "No usable chat model."
            )
        except LLMError as exc:
            info["detail"] = _truncate(_redact(str(exc), self.api_key), 300)
        except Exception as exc:  # pragma: no cover - defensive
            info["detail"] = f"{type(exc).__name__}: {_truncate(_redact(str(exc), self.api_key), 200)}"
        return info


# ---------------------------------------------------------------------------
# Module factory
# ---------------------------------------------------------------------------

_llm_singleton: LLMRouter | None = None


def get_llm() -> LLMRouter:
    """
    Process-wide `LLMRouter` built from settings.

    Shared on purpose: the model cache and the HTTP connection pool are worth
    reusing across requests and background pipeline tasks.
    """
    global _llm_singleton
    if _llm_singleton is None:
        _llm_singleton = LLMRouter()
    return _llm_singleton


async def reset_llm() -> None:
    """Drop the singleton (closing its client) so new settings take effect."""
    global _llm_singleton
    router, _llm_singleton = _llm_singleton, None
    if router is not None:
        await router.aclose()
