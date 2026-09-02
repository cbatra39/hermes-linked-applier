"""
hermes.mcp_client — streamable-HTTP MCP client for `stickerdaniel/linkedin-mcp-server`.

READ THIS BEFORE CALLING ANYTHING HERE
--------------------------------------
Every tool call in this module drives a **real headless Chromium browser** inside
the `linkedin-mcp` container, logged into a real LinkedIn account via the
persistent profile stored in the `linkedin-session` named volume. There is no
LinkedIn API behind it. Consequences you must design around:

  * **It is slow.** A profile scrape is 20-90 s; `search_jobs` with
    `max_pages=10` can exceed five minutes. Timeouts here default to 240 s and
    scale with page count. Never call these on a request thread that a browser
    is waiting on — go through a `Run` + the SSE event stream.
  * **It is serial.** One browser, one tab. Concurrent tool calls interleave
    navigation and corrupt each other's scrapes, so this client serialises calls
    through a semaphore (`max_concurrency`, default 1). Callers that "parallelise"
    enrichment will simply queue.
  * **It is rate-limited by LinkedIn, not by us.** Aggressive scraping earns
    checkpoints and temporary blocks on the user's own account. Enrich the top N
    jobs, not all of them.
  * **There is no apply tool.** The server can read jobs; it cannot submit an
    application. Hermes ranks jobs and hands the human a URL.

Auth model: the session lives in the browser profile volume, established once by
the interactive `--login --login-viewer` one-shot container (noVNC on
LINKEDIN_VIEWER_PORT). An expired/absent session surfaces as a *tool error*
mentioning login/credentials/session — `health()` classifies exactly that case
and distinguishes it from "server unreachable".
"""

from __future__ import annotations

import asyncio
from contextlib import AsyncExitStack, asynccontextmanager
import inspect
import json
import logging
import os
import re
import time
from typing import Any, AsyncIterator
from urllib.parse import urlsplit, urlunsplit

from hermes.settings import settings

try:  # pragma: no cover - the foundation module always exists in the container
    from hermes.events import bus
except Exception:  # pragma: no cover
    bus = None  # type: ignore[assignment]

# The MCP SDK is imported defensively: if the wheel is missing from the image we
# want a clear runtime error and a degraded /api/health, not a crashed process
# that takes the whole dashboard down.
#
# SDK VERSION NOTE (verified live against mcp 2.1.1 + linkedin-mcp-server 4.23.2):
# the 1.x spelling `from mcp.client.streamable_http import streamablehttp_client`
# no longer exists in 2.x — the transport was renamed `streamable_http_client`
# and there is no back-compat alias, so the old import silently disabled every
# LinkedIn call. 2.x also ships a high-level `Client` that takes the endpoint URL
# directly and selects the streamable-HTTP transport itself, which removes all
# the manual read/write-stream plumbing. 2.x is snake_case throughout
# (`is_error`, `structured_content`, `input_schema`).
_MCP_IMPORT_ERROR: Exception | None = None
try:
    from mcp import Client  # type: ignore
except Exception as _exc:  # pragma: no cover
    Client = None  # type: ignore[assignment]
    _MCP_IMPORT_ERROR = _exc

logger = logging.getLogger("hermes.mcp")

__all__ = [
    "MCPError",
    "MCPUnavailableError",
    "MCPToolError",
    "MCPAuthError",
    "LinkedInMCP",
    "get_mcp",
    "reset_mcp",
    "PROFILE_SECTIONS_DEFAULT",
]

# ---------------------------------------------------------------------------
# Tunables
# ---------------------------------------------------------------------------

#: Default per-call ceiling. The server's own TOOL_TIMEOUT is 180 s, so 240 s
#: leaves headroom for browser startup and the HTTP round trip.
DEFAULT_TIMEOUT_S = 240.0

#: Hard ceiling for the page-count-scaled timeouts.
MAX_TIMEOUT_S = 900.0

#: Connect/handshake timeout for the streamable-HTTP transport.
CONNECT_TIMEOUT_S = 20.0

#: Cheap probes (tools/list, health) should fail fast rather than hang a page.
PROBE_TIMEOUT_S = 25.0

#: The auth probe scrapes the *minimum* possible: top card only, no scrolling.
AUTH_PROBE_TIMEOUT_S = 90.0

#: health() result cache — the dashboard polls, LinkedIn does not need to know.
HEALTH_CACHE_TTL_S = 45.0

#: Sections string used by Hermes' profile import (contract wording).
PROFILE_SECTIONS_DEFAULT = "experience,education,skills,certifications,projects"

#: Substrings that mean "the browser is up but LinkedIn will not let us in".
#:
#: The first entry is the verbatim message this server returns for an
#: unauthenticated call, confirmed by probing a live
#: stickerdaniel/linkedin-mcp-server:4.23.2 container with no session:
#:   "No valid LinkedIn session is available in Docker. Create one with the
#:    explicit --login --login-viewer Docker command, or run --login on the
#:    host, then retry this tool."
#: The generic markers below it stay as a safety net for other builds.
AUTH_ERROR_MARKERS: tuple[str, ...] = (
    "no valid linkedin session",
    "login", "log in", "sign in", "signin", "not authenticated", "unauthenticated",
    "authentication", "credential", "cookie", "li_at", "session expired",
    "no session", "invalid session", "session not", "auth wall", "authwall",
    "captcha", "challenge", "checkpoint", "two-factor", "2fa", "verification code",
    "unauthorized", "403", "please authenticate", "run with --login",
)

#: Substrings that mean "not logged-in" is NOT the explanation.
_NON_AUTH_HINTS: tuple[str, ...] = (
    "not found", "does not exist", "no such job", "invalid job id",
    "validation error", "unexpected keyword", "missing required",
)


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------

class MCPError(RuntimeError):
    """Base class for every failure raised by this module."""


class MCPUnavailableError(MCPError):
    """The MCP server could not be reached (or the SDK is not installed)."""


class MCPToolError(MCPError):
    """A tool ran and reported failure (`CallToolResult.is_error`)."""

    def __init__(self, tool: str, message: str) -> None:
        super().__init__(f"MCP tool '{tool}' failed: {message}")
        self.tool = tool
        self.detail = message


class MCPAuthError(MCPToolError):
    """A tool failed specifically because the LinkedIn session is not valid."""


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _setting(name: str, default: Any = None) -> Any:
    """Read a Hermes setting, falling back to the raw environment."""
    value = getattr(settings, name, None)
    if value in (None, ""):
        value = os.getenv(name.upper())
    return default if value in (None, "") else value


def _truncate(text: str, limit: int = 400) -> str:
    text = " ".join((text or "").split())
    return text if len(text) <= limit else text[:limit] + "…"


def _flatten_exception(exc: BaseException, depth: int = 0) -> str:
    """
    Render an exception (including anyio/asyncio ExceptionGroups, which the MCP
    transport raises freely) as one readable line.
    """
    if depth > 4:
        return type(exc).__name__
    group = getattr(exc, "exceptions", None)
    if isinstance(group, (list, tuple)) and group:
        inner = "; ".join(_flatten_exception(e, depth + 1) for e in group[:3])
        return f"{type(exc).__name__}[{inner}]"
    message = str(exc).strip()
    return f"{type(exc).__name__}: {message}" if message else type(exc).__name__


def _looks_like_bad_request(text: str) -> bool:
    """
    True when the server rejected our *arguments* rather than failing to scrape.

    FastMCP validates tool arguments against a Pydantic model and returns the
    validation error as a tool error, so a Hermes-side schema mistake is
    indistinguishable from a scrape failure unless we look for these markers.
    """
    low = (text or "").lower()
    return any(
        marker in low
        for marker in (
            "validation error",
            "unexpected keyword",
            "missing required",
            "input should be",
            "field required",
            "extra inputs are not permitted",
        )
    )


def _looks_like_auth_problem(text: str) -> bool:
    """Classify a tool error message as 'LinkedIn session invalid' or not."""
    low = (text or "").lower()
    if any(hint in low for hint in _NON_AUTH_HINTS):
        return False
    return any(marker in low for marker in AUTH_ERROR_MARKERS)


def _prune(arguments: dict[str, Any]) -> dict[str, Any]:
    """
    Drop None values.

    FastMCP validates arguments against the tool's Pydantic model; sending
    explicit nulls for optional-with-default params is rejected by some versions,
    while omitting them is always correct.
    """
    return {k: v for k, v in arguments.items() if v is not None}


def _content_items(result: Any) -> list[Any]:
    content = getattr(result, "content", None)
    if content is None and isinstance(result, dict):
        content = result.get("content")
    if content is None:
        return []
    return list(content) if isinstance(content, (list, tuple)) else [content]


def _text_from_content(result: Any) -> str:
    """
    Concatenate the text of every TextContent block.

    Real shapes seen from this server: a single TextContent holding a JSON string;
    several TextContent blocks that must be joined before parsing; and (on error)
    a TextContent holding a plain-English traceback summary.
    """
    parts: list[str] = []
    for item in _content_items(result):
        if isinstance(item, str):
            parts.append(item)
            continue
        text = getattr(item, "text", None)
        if text is None and isinstance(item, dict):
            text = item.get("text")
        if text is not None:
            parts.append(str(text))
            continue
        # Non-text block (image / embedded resource): describe it, don't lose it.
        kind = getattr(item, "type", None) or (item.get("type") if isinstance(item, dict) else None)
        if kind:
            parts.append(f"<{kind} content omitted>")
    return "\n".join(p for p in parts if p).strip()


def _structured(result: Any) -> dict[str, Any] | None:
    """Prefer the MCP `structuredContent` field when the server provides it."""
    for attr in ("structuredContent", "structured_content"):
        value = getattr(result, attr, None)
        if value is None and isinstance(result, dict):
            value = result.get(attr)
        if isinstance(value, dict) and value:
            return value
    return None


def _is_error(result: Any) -> bool:
    for attr in ("isError", "is_error"):
        value = getattr(result, attr, None)
        if value is None and isinstance(result, dict):
            value = result.get(attr)
        if value is not None:
            return bool(value)
    return False


def _coerce_payload(text: str) -> dict[str, Any]:
    """
    Turn a tool's text payload into a dict.

    JSON object -> itself. JSON array -> {"items": [...]}. Anything else (plain
    prose, a markdown blob) -> {"text": ...}, per the build contract.
    """
    if not text:
        return {"text": ""}
    stripped = text.strip()
    # Tolerate a fenced JSON payload, which some tool authors emit.
    if stripped.startswith("```"):
        body = stripped.split("```", 2)
        candidate = body[1] if len(body) > 1 else stripped
        candidate = candidate.split("\n", 1)[1] if "\n" in candidate else candidate
        stripped = candidate.strip().rstrip("`").strip() or stripped
    if stripped[:1] in "{[":
        try:
            parsed = json.loads(stripped)
        except json.JSONDecodeError:
            return {"text": text}
        if isinstance(parsed, dict):
            return parsed
        if isinstance(parsed, list):
            return {"items": parsed}
        return {"value": parsed}
    return {"text": text}


# ---------------------------------------------------------------------------
# Client
# ---------------------------------------------------------------------------

class LinkedInMCP:
    """
    Async client for the LinkedIn MCP server over streamable HTTP.

    A fresh MCP session is opened per call (`initialize` -> `tools/call` -> close).
    That is deliberate: the server holds the browser, not the session, so
    per-call sessions cost one cheap handshake and buy immunity to stale
    connections, container restarts and half-open SSE streams — which matter far
    more when a single scrape can run for minutes.

    Usage:
        mcp = get_mcp()
        profile = await mcp.get_my_profile()

        # or scoped (identical behaviour; the ctx manager exists for symmetry
        # with other Hermes resources and for future pooled transports):
        async with LinkedInMCP() as mcp:
            jobs = await mcp.search_jobs("data engineer", location="Bengaluru")
    """

    def __init__(
        self,
        url: str | None = None,
        *,
        timeout: float | None = None,
        headers: dict[str, str] | None = None,
        max_concurrency: int = 1,
    ) -> None:
        raw = str(url or _setting("linkedin_mcp_url") or "").strip()
        if not raw:
            raise MCPError(
                "LINKEDIN_MCP_URL is not set. Expected http://linkedin-mcp:8000/mcp "
                "(service name inside the `hermes` compose network)."
            )
        self.url = self._normalise_url(raw)
        self.timeout = float(timeout or DEFAULT_TIMEOUT_S)
        self.headers = {"User-Agent": "hermes-core/1.0 (+linkedin-mcp)", **(headers or {})}
        # One browser upstream => serialise. See module docstring.
        self._gate = asyncio.Semaphore(max(1, int(max_concurrency)))
        self._health_cache: dict[str, Any] | None = None
        self._health_cached_at: float = 0.0
        self._tool_names: list[str] | None = None
        logger.info("LinkedInMCP target=%s timeout=%ss concurrency=%s", self.url, self.timeout, max_concurrency)

    # -- plumbing ----------------------------------------------------------

    @staticmethod
    def _normalise_url(url: str) -> str:
        """
        Accept `http://host:8000`, `http://host:8000/`, or `http://host:8000/mcp`;
        always yield an explicit endpoint path (defaulting to the compose-configured
        `--path /mcp`). A URL with no scheme is assumed to be plain HTTP, since this
        hop never leaves the compose network.
        """
        raw = (url or "").strip()
        if "://" not in raw:
            raw = "http://" + raw.lstrip("/")
        parts = urlsplit(raw.rstrip("/"))
        path = parts.path or ""
        if not path or path == "/":
            path = "/mcp"
        return urlunsplit((parts.scheme, parts.netloc, path, parts.query, ""))

    def _require_sdk(self) -> None:
        if Client is None:
            raise MCPUnavailableError(
                "The `mcp` Python package (>=2.0) is not importable in hermes-core, so the "
                f"LinkedIn MCP client cannot run (import error: {_MCP_IMPORT_ERROR}). Add `mcp` "
                "to services/core/requirements.txt and rebuild the image."
            )

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

    def _timeout_for_pages(self, pages: int | None, base: float | None = None) -> float:
        """Scale the timeout with the number of result pages the server must scroll."""
        base = float(base or self.timeout)
        pages = max(1, int(pages or 1))
        return min(MAX_TIMEOUT_S, max(base, 90.0 * pages))

    @asynccontextmanager
    async def _session(self, timeout: float) -> AsyncIterator[Any]:
        """
        Yield a connected high-level MCP client, then tear it down.

        Two ways to reach the server, both valid in mcp 2.x:

        * Handing `Client` the endpoint URL as a plain string makes it build a
          `streamable_http_client` transport itself. Simple, but it owns the
          httpx2 client and there is no hook for custom headers — `Client` has
          no `http_client` parameter (that argument belongs to the transport).
        * Calling `streamable_http_client(...)` ourselves returns an async
          context manager, which satisfies the SDK's `Transport` protocol and so
          can be passed to `Client` directly. This is the only way to set
          headers, and it means *we* own the httpx2 client and must close it —
          the transport only closes clients it created itself.

        We take the second path so the Hermes User-Agent (and any operator
        header, e.g. when the MCP endpoint sits behind an auth proxy) is
        actually sent, and we register the httpx2 client on an exit stack so a
        call per scrape does not leak sockets. If httpx2 is somehow missing we
        fall back to the plain-URL form rather than failing the call.
        """
        self._require_sdk()

        async with AsyncExitStack() as stack:
            target: Any = self.url
            try:
                import httpx2  # type: ignore
                from mcp.client.streamable_http import streamable_http_client  # type: ignore

                http_client = httpx2.AsyncClient(
                    headers=dict(self.headers),
                    timeout=httpx2.Timeout(timeout, connect=CONNECT_TIMEOUT_S),
                )
                await stack.enter_async_context(http_client)
                target = streamable_http_client(self.url, http_client=http_client)
            except Exception as exc:  # pragma: no cover - headers are a nicety
                logger.debug(
                    "Falling back to the plain-URL transport (custom headers "
                    "will not be sent): %s",
                    _flatten_exception(exc),
                )

            client = await stack.enter_async_context(
                Client(target, read_timeout_seconds=timeout)
            )
            yield client

    async def __aenter__(self) -> "LinkedInMCP":
        return self

    async def __aexit__(self, *_exc: Any) -> None:
        # Nothing to tear down: sessions are per-call. The remote *browser*
        # session is intentionally left alive so the next call is not forced to
        # cold-start Chromium; use close_session() to release it explicitly.
        return None

    # -- raw call ----------------------------------------------------------

    async def call(
        self,
        tool: str,
        arguments: dict[str, Any] | None = None,
        *,
        timeout: float | None = None,
        run_id: str | None = None,
    ) -> dict[str, Any]:
        """
        Invoke one MCP tool and return its payload as a dict.

        **This drives a real browser.** Expect tens of seconds.

        Raises:
            MCPUnavailableError -- server unreachable, handshake failed, or timeout.
            MCPAuthError        -- tool error whose text indicates the LinkedIn
                                   session is missing/expired/challenged.
            MCPToolError        -- any other tool-reported failure.
        """
        self._require_sdk()
        args = _prune(arguments or {})
        budget = float(timeout or self.timeout)
        started = time.perf_counter()

        async with self._gate:
            await self._emit(run_id, f"LinkedIn MCP -> {tool}({_truncate(json.dumps(args, default=str), 180)})")
            try:
                result = await asyncio.wait_for(self._call_once(tool, args, budget), timeout=budget + 20.0)
            except asyncio.TimeoutError as exc:
                elapsed = int(time.perf_counter() - started)
                await self._emit(run_id, f"LinkedIn MCP {tool} timed out after {elapsed}s", "error")
                raise MCPUnavailableError(
                    f"MCP tool '{tool}' timed out after {elapsed}s (budget {budget:.0f}s). "
                    "LinkedIn scraping is slow; raise the timeout, lower max_pages, or check "
                    "the linkedin-mcp container logs for a stuck browser."
                ) from exc
            except MCPError:
                raise
            except Exception as exc:
                detail = _flatten_exception(exc)
                await self._emit(run_id, f"LinkedIn MCP transport error on {tool}: {detail}", "error")
                raise MCPUnavailableError(
                    f"Could not reach the LinkedIn MCP server at {self.url} ({detail}). "
                    "Is the `linkedin-mcp` container running with "
                    "--transport streamable-http --host 0.0.0.0 --port 8000 --path /mcp ?"
                ) from exc

        elapsed_ms = int((time.perf_counter() - started) * 1000)

        if _is_error(result):
            message = _text_from_content(result) or "tool reported an error with no message"
            if _looks_like_auth_problem(message):
                await self._emit(run_id, f"LinkedIn session not authenticated ({tool}): {_truncate(message, 200)}", "error")
                raise MCPAuthError(
                    tool,
                    f"{_truncate(message, 300)} -- the LinkedIn browser session is missing or expired. "
                    "Run the interactive login one-shot container (--login --login-viewer) and complete "
                    "sign-in at the noVNC viewer, then retry.",
                )
            await self._emit(run_id, f"LinkedIn MCP {tool} error: {_truncate(message, 200)}", "error")
            raise MCPToolError(tool, _truncate(message, 600))

        payload = _structured(result)
        if payload is None:
            payload = _coerce_payload(_text_from_content(result))
        size = len(json.dumps(payload, default=str)) if payload else 0
        await self._emit(run_id, f"LinkedIn MCP <- {tool} ok in {elapsed_ms} ms ({size} bytes)")
        return payload

    async def _call_once(self, tool: str, args: dict[str, Any], budget: float) -> Any:
        """One handshake + one tools/call. Kept separate so wait_for can bound it."""
        async with self._session(budget) as client:
            return await client.call_tool(tool, args, read_timeout_seconds=budget)

    async def list_tools(self, *, timeout: float | None = None) -> list[str]:
        """
        Names of the tools the server advertises. Cheap: no browser involved,
        which makes it the right reachability probe.
        """
        self._require_sdk()
        budget = float(timeout or PROBE_TIMEOUT_S)

        async def _run() -> list[str]:
            async with self._session(budget) as client:
                listing = await client.list_tools()
                tools = getattr(listing, "tools", None) or []
                return [
                    str(getattr(t, "name", "") or (t.get("name") if isinstance(t, dict) else ""))
                    for t in tools
                ]

        try:
            names = await asyncio.wait_for(_run(), timeout=budget + 10.0)
        except asyncio.TimeoutError as exc:
            raise MCPUnavailableError(
                f"tools/list against {self.url} timed out after {budget:.0f}s."
            ) from exc
        except MCPError:
            raise
        except Exception as exc:
            raise MCPUnavailableError(
                f"Could not list tools at {self.url}: {_flatten_exception(exc)}"
            ) from exc

        self._tool_names = [n for n in names if n]
        return list(self._tool_names)

    # -- typed helpers -----------------------------------------------------

    async def get_my_profile(
        self,
        sections: str | None = PROFILE_SECTIONS_DEFAULT,
        max_scrolls: int | None = None,
        *,
        timeout: float | None = None,
        run_id: str | None = None,
    ) -> dict[str, Any]:
        """
        Scrape the logged-in user's own profile. Slow (full page + section scrolls).

        `sections` is the server's comma-ish selector string, e.g.
        "experience,education,skills,certifications,projects".
        """
        return await self.call(
            "get_my_profile",
            {"sections": sections, "max_scrolls": max_scrolls},
            timeout=timeout or max(self.timeout, 300.0),
            run_id=run_id,
        )

    async def get_person_profile(
        self,
        linkedin_username: str,
        sections: str | None = PROFILE_SECTIONS_DEFAULT,
        max_scrolls: int | None = None,
        *,
        timeout: float | None = None,
        run_id: str | None = None,
    ) -> dict[str, Any]:
        """
        Scrape a public profile by its LinkedIn *username* (the vanity slug), not
        a full URL. A pasted URL is reduced to its slug for convenience.
        """
        username = self.normalize_username(linkedin_username)
        if not username:
            raise MCPError("get_person_profile requires a LinkedIn username (the /in/<slug> part).")
        return await self.call(
            "get_person_profile",
            {"linkedin_username": username, "sections": sections, "max_scrolls": max_scrolls},
            timeout=timeout or max(self.timeout, 300.0),
            run_id=run_id,
        )

    @staticmethod
    def normalize_username(value: str | None) -> str:
        """`https://www.linkedin.com/in/jane-doe-123/` -> `jane-doe-123`."""
        raw = (value or "").strip()
        if not raw:
            return ""
        match = re.search(r"/in/([^/?#]+)", raw)
        if match:
            raw = match.group(1)
        return raw.strip("/ ").split("?")[0]

    async def search_jobs(
        self,
        keywords: str,
        location: str | None = None,
        max_pages: int = 3,
        date_posted: str | None = None,
        job_type: str | None = None,
        experience_level: str | None = None,
        work_type: str | None = None,
        easy_apply: bool = False,
        sort_by: str | None = None,
        *,
        timeout: float | None = None,
        run_id: str | None = None,
    ) -> dict[str, Any]:
        """
        Search LinkedIn jobs. `max_pages` is clamped to the server's 1..10 range;
        the timeout scales at ~90 s per page because each page is a scroll+parse.
        """
        if not (keywords or "").strip():
            raise MCPError("search_jobs requires non-empty keywords.")
        pages = max(1, min(10, int(max_pages or 1)))
        return await self.call(
            "search_jobs",
            {
                "keywords": keywords.strip(),
                "location": location,
                "max_pages": pages,
                "date_posted": date_posted,
                "job_type": job_type,
                "experience_level": experience_level,
                "work_type": work_type,
                "easy_apply": bool(easy_apply),
                "sort_by": sort_by,
            },
            timeout=timeout or self._timeout_for_pages(pages),
            run_id=run_id,
        )

    async def get_job_details(
        self,
        job_id: str,
        *,
        timeout: float | None = None,
        run_id: str | None = None,
    ) -> dict[str, Any]:
        """Fetch one job posting's full description. `job_id` is LinkedIn's numeric id."""
        jid = str(job_id or "").strip()
        if not jid:
            raise MCPError("get_job_details requires a LinkedIn job id.")
        return await self.call("get_job_details", {"job_id": jid}, timeout=timeout, run_id=run_id)

    async def get_saved_jobs(
        self,
        max_pages: int = 3,
        *,
        timeout: float | None = None,
        run_id: str | None = None,
    ) -> dict[str, Any]:
        """List the account's saved jobs (browser-driven, paginated)."""
        pages = max(1, min(10, int(max_pages or 1)))
        return await self.call(
            "get_saved_jobs",
            {"max_pages": pages},
            timeout=timeout or self._timeout_for_pages(pages),
            run_id=run_id,
        )

    async def close_session(self, *, run_id: str | None = None) -> dict[str, Any]:
        """
        Ask the server to shut down its browser session, releasing RAM. The next
        tool call cold-starts Chromium again (slower), so use sparingly — e.g. at
        the end of a full pipeline run.
        """
        return await self.call("close_session", {}, timeout=PROBE_TIMEOUT_S, run_id=run_id)

    # -- health ------------------------------------------------------------

    async def health(
        self,
        *,
        probe_auth: bool = True,
        use_cache: bool = True,
        run_id: str | None = None,
    ) -> dict[str, Any]:
        """
        Never-raising status probe. Distinguishes the two failures that matter:

            {"reachable": False, ...}                    -> container down / wrong URL
            {"reachable": True, "authenticated": False}  -> up, but LinkedIn session invalid

        Strategy: `tools/list` first (no browser, fast) for reachability, then —
        when `probe_auth` — the cheapest browser call available
        (`get_my_profile` with no sections and no scrolling) purely to see whether
        the session survives. Results are cached for HEALTH_CACHE_TTL_S because
        the auth probe touches LinkedIn.

        Extra keys beyond the contract's three: `status` (ok | unreachable |
        unauthenticated | degraded | unknown), `tools`, `url`, `checked_at`.
        """
        now = time.monotonic()
        if use_cache and self._health_cache and (now - self._health_cached_at) < HEALTH_CACHE_TTL_S:
            cached = dict(self._health_cache)
            cached["cached"] = True
            return cached

        info: dict[str, Any] = {
            "reachable": False,
            "authenticated": False,
            "detail": "",
            "status": "unknown",
            "tools": [],
            "url": self.url,
            "checked_at": time.time(),
            "cached": False,
        }

        if Client is None:
            info["detail"] = (
                "The `mcp` Python package (>=2.0) is missing from hermes-core "
                f"({_MCP_IMPORT_ERROR}); add it to services/core/requirements.txt."
            )
            info["status"] = "unreachable"
            self._health_cache, self._health_cached_at = dict(info), time.monotonic()
            return info

        # 1) Reachability — no browser touched.
        try:
            info["tools"] = await self.list_tools()
            info["reachable"] = True
        except Exception as exc:
            info["detail"] = (
                f"LinkedIn MCP unreachable at {self.url}: {_truncate(_flatten_exception(exc), 240)}"
            )
            info["status"] = "unreachable"
            self._health_cache, self._health_cached_at = dict(info), time.monotonic()
            return info

        if not probe_auth:
            info["detail"] = f"Server reachable; {len(info['tools'])} tool(s). LinkedIn session not probed."
            info["status"] = "unknown"
            self._health_cache, self._health_cached_at = dict(info), time.monotonic()
            return info

        # 2) Auth — cheapest possible browser call.
        try:
            # `max_scrolls` is validated server-side as ge=1 (le=50), so 0 is
            # rejected before the browser is ever touched — the probe then
            # learns nothing about the session. 1 is the real minimum. Leave
            # `sections` unset (it is pruned) so the server picks its own
            # cheapest default.
            await self.call(
                "get_my_profile",
                {"max_scrolls": 1},
                timeout=AUTH_PROBE_TIMEOUT_S,
                run_id=run_id,
            )
            info["authenticated"] = True
            info["status"] = "ok"
            info["detail"] = f"Server reachable and LinkedIn session valid ({len(info['tools'])} tools)."
        except MCPAuthError as exc:
            info["authenticated"] = False
            info["status"] = "unauthenticated"
            info["detail"] = (
                "Server reachable but the LinkedIn session is not authenticated. "
                "Start the interactive login container (--login --login-viewer) and finish sign-in "
                f"in the noVNC viewer. Server said: {_truncate(exc.detail, 200)}"
            )
        except MCPUnavailableError as exc:
            # A timeout here means the browser was still working. Auth failures
            # from this server come back fast, so a slow probe is evidence *for*
            # a live session, not against it — report it honestly as degraded.
            info["authenticated"] = True
            info["status"] = "degraded"
            info["detail"] = (
                "Server reachable; the login probe did not finish in "
                f"{AUTH_PROBE_TIMEOUT_S:.0f}s. The browser is likely mid-scrape (auth errors "
                f"return immediately), so the session is probably valid. Detail: {_truncate(str(exc), 200)}"
            )
        except MCPToolError as exc:
            if _looks_like_bad_request(exc.detail):
                # The server rejected our arguments before opening a page, so
                # the probe never reached LinkedIn. Claiming "authenticated"
                # here would paint a red light green — this is a Hermes bug,
                # not evidence of a live session.
                info["authenticated"] = False
                info["status"] = "unknown"
                info["detail"] = (
                    "Server reachable, but the auth probe was rejected as an invalid request, so "
                    "the LinkedIn session state is unknown. This is a Hermes bug — the probe "
                    f"arguments do not match the server's schema. Server said: {_truncate(exc.detail, 240)}"
                )
            else:
                # Ran, failed, but not for auth reasons (layout change, rate
                # limit). Session is presumed fine; scraping is not.
                info["authenticated"] = True
                info["status"] = "degraded"
                info["detail"] = (
                    "Server reachable and no login error was reported, but the probe scrape failed: "
                    f"{_truncate(exc.detail, 240)}"
                )
        except Exception as exc:  # pragma: no cover - health() must never raise
            info["authenticated"] = False
            info["status"] = "unknown"
            info["detail"] = f"Unexpected probe failure: {_truncate(_flatten_exception(exc), 240)}"

        self._health_cache, self._health_cached_at = dict(info), time.monotonic()
        return info

    def invalidate_health_cache(self) -> None:
        """Force the next `health()` to re-probe (call after an interactive login)."""
        self._health_cache = None
        self._health_cached_at = 0.0


# ---------------------------------------------------------------------------
# Module factory
# ---------------------------------------------------------------------------

_mcp_singleton: LinkedInMCP | None = None


def get_mcp() -> LinkedInMCP:
    """
    Process-wide `LinkedInMCP`.

    Shared on purpose: the semaphore that serialises browser access and the
    health cache are only meaningful if every caller uses the same instance.
    """
    global _mcp_singleton
    if _mcp_singleton is None:
        _mcp_singleton = LinkedInMCP()
    return _mcp_singleton


def reset_mcp() -> None:
    """Drop the singleton so a changed LINKEDIN_MCP_URL takes effect."""
    global _mcp_singleton
    _mcp_singleton = None
