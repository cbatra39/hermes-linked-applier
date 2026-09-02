"""Base class and shared helpers for every Hermes agent.

An "agent" here is a thin, testable object that owns one LLM-driven (or, in the
case of the ATS scorer, purely deterministic) capability.  Agents never touch
the database or the HTTP layer directly -- ``hermes.pipeline`` wires them
together, persists their output and owns transaction boundaries.

Design rules for subclasses
---------------------------
* Exactly ONE public async method per agent (``analyze`` / ``build`` /
  ``search`` / ``rank``).  Everything else is a private helper.
* Every subclass defines a ``SYSTEM`` prompt as a class attribute so prompts
  are reviewable in one place and can be unit-tested for regressions.
* Progress goes out through ``await self.emit(...)`` which publishes to the
  SSE event bus AND persists a ``RunEvent`` row (see ``hermes.events``).
* LLM output is *never* trusted blindly: each agent normalises/validates the
  model response into the exact contract shape before returning it.
"""

from __future__ import annotations

import inspect
import json
import logging
import re
from typing import TYPE_CHECKING, Any, Iterable, Sequence

# The event bus is a hard dependency: if it cannot be imported the service is
# mis-deployed and we want to know immediately rather than silently losing all
# run telemetry.
from hermes.events import bus

if TYPE_CHECKING:  # pragma: no cover - typing only, avoids import cycles at runtime
    from hermes.llm import LLMRouter
    from hermes.mcp_client import LinkedInMCP

log = logging.getLogger(__name__)

_LOG_LEVELS = {
    "debug": logging.DEBUG,
    "info": logging.INFO,
    "warn": logging.WARNING,
    "warning": logging.WARNING,
    "error": logging.ERROR,
}

__all__ = ["Agent", "AgentError"]


class AgentError(RuntimeError):
    """Raised when an agent cannot fulfil its contract (bad config, dead LLM...)."""


class Agent:
    """Common plumbing shared by all Hermes agents.

    Parameters
    ----------
    llm:
        An ``hermes.llm.LLMRouter``.  Required for LLM-backed agents; agents
        that can operate deterministically (e.g. a pure ATS scan) tolerate
        ``None`` and say so in their docstring.
    mcp:
        An ``hermes.mcp_client.LinkedInMCP`` for agents that scrape LinkedIn.
    run_id:
        The ``Run.id`` this agent is executing under.  When present, every
        ``emit()`` is streamed to the dashboard over SSE and persisted.
    """

    #: Overridden by every subclass.  Kept as a class attribute so prompts are
    #: introspectable (the Settings page can display them) and diff-reviewable.
    SYSTEM: str = (
        "You are a precise, senior-level career-tooling assistant. "
        "You never invent facts and you always answer in the exact format requested."
    )

    #: Short human label used in event-log lines.
    NAME: str = "agent"

    def __init__(
        self,
        llm: "LLMRouter | None" = None,
        mcp: "LinkedInMCP | None" = None,
        run_id: str | None = None,
    ) -> None:
        self.llm = llm
        self.mcp = mcp
        self.run_id = run_id

    # ------------------------------------------------------------------ events

    async def emit(self, msg: str, level: str = "info") -> None:
        """Publish a progress line for this run (never raises).

        Telemetry must not be able to fail a pipeline, so bus errors are logged
        and swallowed.  ``bus.publish`` is supported both as a coroutine and as
        a plain function.
        """
        text = str(msg)
        log.log(_LOG_LEVELS.get(level.lower(), logging.INFO), "[%s] %s", self.NAME, text)
        if not self.run_id:
            return
        try:
            result = bus.publish(self.run_id, level, f"[{self.NAME}] {text}")
            if inspect.isawaitable(result):
                await result
        except Exception as exc:  # pragma: no cover - telemetry is best-effort
            log.warning("event publish failed for run %s: %s", self.run_id, exc)

    # --------------------------------------------------------------------- llm

    def _require_llm(self) -> "LLMRouter":
        if self.llm is None:
            raise AgentError(
                f"{type(self).__name__} requires an LLM router. "
                "Check FREELLMAPI_BASE_URL / FREELLMAPI_KEY in your .env."
            )
        return self.llm

    def _require_mcp(self) -> "LinkedInMCP":
        if self.mcp is None:
            raise AgentError(
                f"{type(self).__name__} requires the LinkedIn MCP client. "
                "Check LINKEDIN_MCP_URL and that the linkedin-mcp container is up."
            )
        return self.mcp

    async def ask(
        self,
        user: str,
        *,
        system: str | None = None,
        temperature: float = 0.2,
        max_tokens: int | None = None,
    ) -> str:
        """Single-turn plain-text LLM call using this agent's SYSTEM prompt."""
        llm = self._require_llm()
        messages = [
            {"role": "system", "content": system or self.SYSTEM},
            {"role": "user", "content": user},
        ]
        return await llm.chat(
            messages,
            temperature=temperature,
            max_tokens=max_tokens,
            run_id=self.run_id,
        )

    async def ask_json(
        self,
        user: str,
        *,
        schema_hint: str,
        system: str | None = None,
        temperature: float = 0.15,
        max_tokens: int | None = None,
    ) -> dict:
        """Single-turn JSON LLM call.  Returns ``{}``-safe dict (never None)."""
        llm = self._require_llm()
        messages = [
            {"role": "system", "content": system or self.SYSTEM},
            {"role": "user", "content": user},
        ]
        data = await llm.chat_json(
            messages,
            schema_hint=schema_hint,
            temperature=temperature,
            max_tokens=max_tokens,
            json_mode=True,
            run_id=self.run_id,
        )
        if isinstance(data, dict):
            return data
        if isinstance(data, list):  # some models wrap the object in an array
            for item in data:
                if isinstance(item, dict):
                    return item
        return {}

    # ----------------------------------------------------------- tiny utilities
    # These are intentionally static + dependency-free so agents (and their
    # tests) can normalise messy scraped payloads without extra imports.

    @staticmethod
    def clean_str(value: Any, limit: int | None = None) -> str:
        """Coerce anything to a trimmed single-spaced string."""
        if value is None or isinstance(value, bool):
            return ""
        if isinstance(value, (int, float)):
            return str(value)
        if isinstance(value, dict):
            for key in ("text", "name", "title", "value", "label"):
                if key in value:
                    return Agent.clean_str(value[key], limit)
            return ""
        if isinstance(value, (list, tuple, set)):
            return ", ".join(x for x in (Agent.clean_str(v) for v in value) if x)[: limit or 100000]
        text = re.sub(r"[ \t ]+", " ", str(value)).strip()
        if limit and len(text) > limit:
            text = text[:limit].rstrip() + "..."
        return text

    @staticmethod
    def as_list(value: Any) -> list[Any]:
        """Coerce a scalar / None / list / dict-of-lists into a flat list."""
        if value is None:
            return []
        if isinstance(value, (list, tuple, set)):
            return [v for v in value]
        if isinstance(value, dict):
            # dict-of-lists (common in scraped payloads) -> concatenate values
            out: list[Any] = []
            for v in value.values():
                out.extend(Agent.as_list(v))
            return out
        return [value]

    @staticmethod
    def str_list(value: Any, *, limit: int | None = None, item_limit: int = 240) -> list[str]:
        """Normalise anything into a de-duplicated list of non-empty strings."""
        seen: set[str] = set()
        out: list[str] = []
        for raw in Agent.as_list(value):
            text = Agent.clean_str(raw, item_limit)
            if not text:
                continue
            key = text.lower()
            if key in seen:
                continue
            seen.add(key)
            out.append(text)
            if limit and len(out) >= limit:
                break
        return out

    @staticmethod
    def truncate(text: str, limit: int) -> str:
        """Hard-truncate prompt material, marking the cut so the model knows."""
        text = text or ""
        if len(text) <= limit:
            return text
        return text[:limit].rstrip() + f"\n...[truncated, {len(text) - limit} chars omitted]"

    @staticmethod
    def compact_json(obj: Any, limit: int = 12000) -> str:
        """JSON for prompt embedding: compact, non-ASCII preserved, truncated."""
        try:
            text = json.dumps(obj, ensure_ascii=False, default=str, separators=(",", ":"))
        except Exception:
            text = str(obj)
        return Agent.truncate(text, limit)

    @staticmethod
    def first_of(data: Any, *keys: str, default: Any = None) -> Any:
        """Return the first present, non-empty value among ``keys`` in a dict.

        Key lookup is case/separator insensitive (``easy_apply`` matches
        ``easyApply``), which is what makes the scraped-payload normalisers in
        ``job_scout`` resilient to upstream shape changes.
        """
        if not isinstance(data, dict):
            return default
        normalised = {re.sub(r"[^a-z0-9]", "", str(k).lower()): v for k, v in data.items()}
        for key in keys:
            probe = re.sub(r"[^a-z0-9]", "", key.lower())
            if probe in normalised:
                value = normalised[probe]
                if value not in (None, "", [], {}):
                    return value
        return default

    @staticmethod
    def clamp(value: Any, lo: float = 0.0, hi: float = 100.0, default: float = 0.0) -> float:
        """Coerce a model-supplied number into a bounded float."""
        try:
            num = float(value)
        except (TypeError, ValueError):
            return default
        if num != num or num in (float("inf"), float("-inf")):  # NaN / inf guard
            return default
        return max(lo, min(hi, num))

    @staticmethod
    def bullet_lines(items: Iterable[str], bullet: str = "- ") -> str:
        """Render an iterable as markdown bullet lines."""
        return "\n".join(f"{bullet}{item}" for item in items if str(item).strip())

    @staticmethod
    def join_nonempty(parts: Sequence[Any], sep: str = " | ") -> str:
        return sep.join(p for p in (Agent.clean_str(x) for x in parts) if p)
