"""Run event bus: in-process fan-out + durable RunEvent rows + SSE framing.

DESIGN NOTES (why it cannot deadlock or leak):
  * `publish()` is fully SYNCHRONOUS and never blocks. It uses
    `Queue.put_nowait`; when a subscriber's queue is full the OLDEST item is
    dropped rather than making the producer wait. A slow browser therefore can
    never stall a pipeline.
  * `publish()` is nonetheless *awaitable*. It returns a tiny already-completed
    awaitable, so BOTH styles work and neither warns::
        bus.publish(run_id, "info", "hello")
        await bus.publish(run_id, "info", "hello")      # e.g. from Agent.emit()
    This is intentional: the contract types it as a plain method, but agent code
    naturally awaits its `emit()` helpers.
  * Zero subscribers is the normal case (nobody has the Runs page open). Events
    are still persisted and appended to a bounded in-memory ring buffer so a
    late subscriber gets a replay instead of an empty screen.
  * Cross-thread safe: queues are registered together with the loop that owns
    them, and delivery from a non-owning thread goes through
    `loop.call_soon_threadsafe`. Pipelines that do DB work in `asyncio.to_thread`
    can log from there safely.
  * HEARTBEAT: `subscribe()` yields an SSE comment line (`: ping`) every
    `HEARTBEAT_SECONDS` of silence. Without it, idle proxies (nginx in the
    dashboard image, corporate middleboxes) close the connection after ~60s and
    the live log silently dies. Comment lines are ignored by EventSource but
    keep the socket warm and let us notice a disconnected client.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from collections import deque
from collections.abc import AsyncIterator
from typing import Any, Deque

log = logging.getLogger("hermes.events")

# Level string used to mark "this run is finished, close the stream".
TERMINAL_LEVEL = "end"

MAX_MESSAGE_CHARS = 8000  # guardrail: never persist a megabyte of LLM output


class _Completed:
    """An awaitable that is already done.

    Lets `publish()` be called with or without `await` (see module docstring)
    without producing a "coroutine was never awaited" RuntimeWarning.
    """

    __slots__ = ("value",)

    def __init__(self, value: Any = None) -> None:
        self.value = value

    def __await__(self):  # type: ignore[no-untyped-def]
        yield from ()  # never suspends
        return self.value

    def __bool__(self) -> bool:
        return True


class _Subscriber:
    """One live SSE consumer: its queue plus the loop that queue belongs to."""

    __slots__ = ("queue", "loop")

    def __init__(self, queue: "asyncio.Queue[dict[str, Any] | None]", loop: asyncio.AbstractEventLoop) -> None:
        self.queue = queue
        self.loop = loop


def sse_format(
    data: Any,
    *,
    event: str | None = None,
    event_id: str | int | None = None,
    retry_ms: int | None = None,
) -> str:
    """Frame `data` as a Server-Sent Events message (trailing blank line incl.).

    dicts/lists are JSON-encoded; anything else is str()'d. Embedded newlines
    are split across multiple `data:` lines as the SSE spec requires — miss this
    and a multi-line traceback silently truncates the stream.
    """
    lines: list[str] = []
    if event:
        lines.append(f"event: {event}")
    if event_id is not None:
        lines.append(f"id: {event_id}")
    if retry_ms is not None:
        lines.append(f"retry: {int(retry_ms)}")

    if isinstance(data, (dict, list, tuple)):
        payload = json.dumps(data, ensure_ascii=False, default=str)
    else:
        payload = "" if data is None else str(data)

    for chunk in payload.split("\n"):
        lines.append(f"data: {chunk}")
    return "\n".join(lines) + "\n\n"


def sse_comment(text: str = "ping") -> str:
    """A `: comment` keep-alive line (ignored by EventSource clients)."""
    return f": {text}\n\n"


class EventBus:
    """Fan-out of run progress events to SSE subscribers, with DB persistence."""

    HEARTBEAT_SECONDS: float = 15.0
    QUEUE_MAXSIZE: int = 512
    HISTORY_MAXLEN: int = 500

    def __init__(self) -> None:
        self._subscribers: dict[str, list[_Subscriber]] = {}
        self._history: dict[str, Deque[dict[str, Any]]] = {}
        self._closed: set[str] = set()

    # ------------------------------------------------------------------ #
    # producing
    # ------------------------------------------------------------------ #
    def publish(
        self,
        run_id: str | None,
        level: str = "info",
        message: str = "",
        *,
        persist: bool = True,
        extra: dict[str, Any] | None = None,
    ) -> _Completed:
        """Record + broadcast one event. Never raises, never blocks.

        Args:
            run_id: Run.id this event belongs to. None => log only (no fan-out).
            level: debug|info|warn|error|end.
            message: human-readable progress line (truncated at 8k chars).
            persist: write a RunEvent row (turn off for high-frequency chatter
                like container log tailing).
            extra: additional JSON-serialisable fields merged into the payload.

        Returns:
            An already-completed awaitable, so `await bus.publish(...)` is legal.
        """
        lvl = (level or "info").strip().lower()
        msg = message if isinstance(message, str) else str(message)
        if len(msg) > MAX_MESSAGE_CHARS:
            msg = msg[:MAX_MESSAGE_CHARS] + f"... [truncated {len(msg) - MAX_MESSAGE_CHARS} chars]"

        log.log(_log_level(lvl), "[run %s] %s", run_id or "-", msg)

        if not run_id:
            return _Completed()

        payload: dict[str, Any] = {
            "run_id": run_id,
            "ts": time.time(),
            "level": lvl,
            "message": msg,
        }
        if extra:
            payload.update(extra)

        if persist:
            self._persist(run_id, lvl, msg)

        self._history.setdefault(run_id, deque(maxlen=self.HISTORY_MAXLEN)).append(payload)
        self._fanout(run_id, payload)
        return _Completed()

    def close(self, run_id: str | None, message: str = "run finished") -> _Completed:
        """Emit a terminal event and end every open stream for this run.

        Subscribers see the final message, then their `async for` loop exits so
        the HTTP response can complete instead of hanging until the client
        gives up.
        """
        if not run_id:
            return _Completed()
        self.publish(run_id, TERMINAL_LEVEL, message)
        self._closed.add(run_id)
        for sub in list(self._subscribers.get(run_id, ())):
            self._deliver(sub, None)  # None = end-of-stream sentinel
        return _Completed()

    # ------------------------------------------------------------------ #
    # consuming
    # ------------------------------------------------------------------ #
    async def subscribe(self, run_id: str, *, replay: bool = True) -> AsyncIterator[str]:
        """Yield SSE-formatted strings for `run_id` until the run closes.

        Async generator, so routes can do::

            return StreamingResponse(bus.subscribe(run_id),
                                     media_type="text/event-stream")

        Cancellation (client disconnect) unregisters the queue in the `finally`
        block, so there is no subscriber leak.
        """
        queue: "asyncio.Queue[dict[str, Any] | None]" = asyncio.Queue(maxsize=self.QUEUE_MAXSIZE)
        sub = _Subscriber(queue, asyncio.get_running_loop())
        self._subscribers.setdefault(run_id, []).append(sub)

        try:
            # Tell the browser how long to wait before reconnecting, and flush
            # headers immediately so the UI shows "connected" right away.
            yield sse_format({"run_id": run_id, "level": "info", "message": "stream open",
                              "ts": time.time()}, event="open", retry_ms=3000)

            if replay:
                for past in list(self._history.get(run_id, ())):
                    yield sse_format(past)
                if run_id in self._closed:
                    # Run already finished before anyone subscribed: replay then
                    # end cleanly rather than hanging on an empty queue.
                    yield sse_format(
                        {"run_id": run_id, "level": TERMINAL_LEVEL, "message": "run finished",
                         "ts": time.time()},
                        event=TERMINAL_LEVEL,
                    )
                    return

            while True:
                try:
                    item = await asyncio.wait_for(queue.get(), timeout=self.HEARTBEAT_SECONDS)
                except asyncio.TimeoutError:
                    # Idle: keep the connection alive through proxies.
                    yield sse_comment("ping")
                    continue

                if item is None:  # end-of-stream sentinel from close()
                    yield sse_format(
                        {"run_id": run_id, "level": TERMINAL_LEVEL, "message": "run finished",
                         "ts": time.time()},
                        event=TERMINAL_LEVEL,
                    )
                    return

                yield sse_format(item, event=item.get("level") or "message")
        finally:
            subs = self._subscribers.get(run_id)
            if subs is not None:
                try:
                    subs.remove(sub)
                except ValueError:
                    pass
                if not subs:
                    self._subscribers.pop(run_id, None)

    def history(self, run_id: str) -> list[dict[str, Any]]:
        """Buffered events for a run (most recent `HISTORY_MAXLEN`)."""
        return list(self._history.get(run_id, ()))

    def subscriber_count(self, run_id: str | None = None) -> int:
        """Live subscriber count, for a run or across all runs."""
        if run_id is not None:
            return len(self._subscribers.get(run_id, ()))
        return sum(len(v) for v in self._subscribers.values())

    def forget(self, run_id: str) -> None:
        """Drop buffered history/closed marker for a run (housekeeping)."""
        self._history.pop(run_id, None)
        self._closed.discard(run_id)

    # ------------------------------------------------------------------ #
    # internals
    # ------------------------------------------------------------------ #
    def _fanout(self, run_id: str, payload: dict[str, Any]) -> None:
        for sub in list(self._subscribers.get(run_id, ())):
            self._deliver(sub, payload)

    def _deliver(self, sub: _Subscriber, item: dict[str, Any] | None) -> None:
        """Put `item` on a subscriber queue without ever blocking the producer."""
        try:
            running = asyncio.get_running_loop()
        except RuntimeError:
            running = None

        if running is sub.loop:
            self._put_nowait_drop_oldest(sub.queue, item)
            return

        # Different (or no) loop: hop threads. If the target loop is gone the
        # subscriber is already dead and we simply drop the event.
        try:
            sub.loop.call_soon_threadsafe(self._put_nowait_drop_oldest, sub.queue, item)
        except RuntimeError:
            pass

    @staticmethod
    def _put_nowait_drop_oldest(
        queue: "asyncio.Queue[dict[str, Any] | None]", item: dict[str, Any] | None
    ) -> None:
        try:
            queue.put_nowait(item)
        except asyncio.QueueFull:
            # Slow consumer: shed the oldest event, keep the newest. Better a
            # gap in the log than a blocked pipeline.
            try:
                queue.get_nowait()
            except asyncio.QueueEmpty:  # pragma: no cover - race
                pass
            try:
                queue.put_nowait(item)
            except asyncio.QueueFull:  # pragma: no cover - race
                pass

    @staticmethod
    def _persist(run_id: str, level: str, message: str) -> None:
        """Append a RunEvent row. Imported lazily to avoid an import cycle and
        to keep `hermes.events` importable without touching the DB.

        Any DB failure is logged and swallowed: losing a log line must never
        fail the pipeline that was reporting progress.
        """
        try:
            from hermes.db import SessionLocal
            from hermes.models import RunEvent

            db = SessionLocal()
            try:
                db.add(RunEvent(run_id=run_id, level=level, message=message))
                db.commit()
            finally:
                db.close()
        except Exception as exc:  # noqa: BLE001 - never propagate
            log.debug("Could not persist RunEvent for run %s: %s", run_id, exc)


def _log_level(level: str) -> int:
    return {
        "debug": logging.DEBUG,
        "info": logging.INFO,
        "warn": logging.WARNING,
        "warning": logging.WARNING,
        "error": logging.ERROR,
        "end": logging.INFO,
    }.get(level, logging.INFO)


# Module singleton — import this, do not instantiate EventBus elsewhere.
bus = EventBus()

__all__ = ["TERMINAL_LEVEL", "EventBus", "bus", "sse_comment", "sse_format"]
