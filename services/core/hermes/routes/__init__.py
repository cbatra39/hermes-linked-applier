"""Router registry for the Hermes HTTP API.

``hermes.main`` mounts everything returned by :func:`load_routers` under
``/api``. Keeping the registry here (rather than a hand-written import list in
``main.py``) means a new route module is wired up by adding one line to
:data:`ROUTE_SLOTS`.

Two deliberate design points:

* **Eager vs. lazy.** The modules that ship with this package are imported
  eagerly so ``from hermes.routes import health, llm, linkedin, runs, settings``
  works exactly like a normal attribute access, and so an import-time syntax
  error surfaces the moment the package is loaded instead of on the first
  request. The remaining slots are resolved by :func:`load_routers` at
  ``create_app()`` time.
* **Loud, actionable failure.** A required route module that cannot be imported
  raises :class:`RouteModuleError` naming the file that is missing and the API
  paths that would silently 404 without it. Hermes never quietly serves a
  half-mounted API — a dashboard page hitting a missing endpoint is much harder
  to diagnose than a container that refuses to start with a one-line reason.

Every module in a slot must expose a module-level ``router: APIRouter`` whose
paths are relative to ``/api`` (e.g. ``@router.get("/runs")``).
"""

from __future__ import annotations

import importlib
import logging
from types import ModuleType

from fastapi import APIRouter

# Eager imports: these five modules are owned by this package and always exist.
# (Imported before ROUTE_SLOTS is used so a typo here fails at import time.)
from hermes.routes import health as health
from hermes.routes import linkedin as linkedin
from hermes.routes import llm as llm
from hermes.routes import runs as runs
from hermes.routes import settings as settings

log = logging.getLogger("hermes.api")

__all__ = [
    "ROUTE_SLOTS",
    "RouteModuleError",
    "health",
    "linkedin",
    "llm",
    "load_routers",
    "router_modules",
    "runs",
    "settings",
]


class RouteModuleError(RuntimeError):
    """A required route module is missing, unimportable, or has no ``router``."""


#: ``(slot, candidate module names, required, endpoints served)``.
#:
#: The candidate tuple exists so a singular/plural naming difference between
#: parallel authors cannot take the whole API down; the first importable name
#: wins. ``endpoints`` is only used to build a useful error message.
ROUTE_SLOTS: tuple[tuple[str, tuple[str, ...], bool, str], ...] = (
    ("health", ("health",), True, "GET /api/health"),
    ("settings", ("settings",), True, "GET|PUT /api/settings"),
    ("llm", ("llm",), True, "GET /api/llm/models, POST /api/llm/test"),
    ("linkedin", ("linkedin",), True, "GET /api/linkedin/status, POST /api/linkedin/login"),
    ("profile", ("profile", "profiles"), True, "POST /api/profile/import, GET /api/profile"),
    (
        "resume",
        ("resume", "resumes"),
        True,
        "POST /api/resume/upload|generate|score, GET /api/resumes*",
    ),
    ("jobs", ("jobs", "job"), True, "POST /api/jobs/search, GET|PATCH /api/jobs*"),
    ("runs", ("runs", "run"), True, "GET /api/runs*, GET /api/runs/{id}/events (SSE)"),
    (
        "containers",
        ("containers", "container"),
        True,
        "GET /api/containers*, POST /api/containers/{id}/start|stop|restart, POST /api/sandbox/exec",
    ),
    # Optional: POST /api/sandbox/exec may live in containers.py instead of its
    # own module. Absence is not an error; a present module is mounted.
    ("sandbox", ("sandbox",), False, "POST /api/sandbox/exec"),
)


def _import_slot(candidates: tuple[str, ...]) -> tuple[ModuleType | None, dict[str, str]]:
    """Import the first importable candidate. Returns ``(module, failures)``."""
    failures: dict[str, str] = {}
    for name in candidates:
        try:
            return importlib.import_module(f"{__name__}.{name}"), failures
        except ModuleNotFoundError as exc:
            # Only "this module does not exist" is a candidate miss; a
            # ModuleNotFoundError raised *inside* the module (a missing
            # dependency) must not be mistaken for an absent route file.
            if exc.name in (f"{__name__}.{name}", name):
                failures[name] = "not found"
                continue
            failures[name] = f"{type(exc).__name__}: {exc}"
            raise RouteModuleError(
                f"hermes/routes/{name}.py exists but could not be imported: {exc}. "
                "This is a missing dependency inside the route module, not a missing file."
            ) from exc
        except Exception as exc:  # noqa: BLE001 - re-raised with context below
            raise RouteModuleError(
                f"hermes/routes/{name}.py failed to import: {type(exc).__name__}: {exc}"
            ) from exc
    return None, failures


def router_modules() -> list[tuple[str, ModuleType]]:
    """Resolve every slot to a module. Raises :class:`RouteModuleError` loudly."""
    resolved: list[tuple[str, ModuleType]] = []
    for slot, candidates, required, endpoints in ROUTE_SLOTS:
        module, failures = _import_slot(candidates)
        if module is None:
            if not required:
                log.debug("optional route module %s not present (%s)", slot, failures)
                continue
            expected = " or ".join(f"hermes/routes/{name}.py" for name in candidates)
            raise RouteModuleError(
                f"Required route module for '{slot}' is missing: expected {expected}. "
                f"Without it these endpoints would 404: {endpoints}. "
                f"Tried: {', '.join(f'{k} ({v})' for k, v in failures.items())}."
            )
        resolved.append((slot, module))
    return resolved


def load_routers() -> list[tuple[str, APIRouter]]:
    """``[(slot, router)]`` for every mountable route module, in mount order."""
    routers: list[tuple[str, APIRouter]] = []
    seen: set[int] = set()
    for slot, module in router_modules():
        candidate = getattr(module, "router", None)
        if not isinstance(candidate, APIRouter):
            raise RouteModuleError(
                f"{module.__name__} does not expose a module-level "
                "`router: APIRouter`. Every Hermes route module must define one "
                "(see hermes/routes/health.py for the established shape)."
            )
        if id(candidate) in seen:  # two slots resolved to the same module object
            continue
        seen.add(id(candidate))
        routers.append((slot, candidate))
    return routers


def __getattr__(name: str) -> ModuleType:
    """Import ``hermes.routes.<name>`` on first attribute access.

    Makes ``hermes.routes.jobs`` work before ``load_routers()`` has run, with a
    message that names the file instead of the bare attribute.
    """
    known = {candidate for _, candidates, _, _ in ROUTE_SLOTS for candidate in candidates}
    if name in known:
        try:
            return importlib.import_module(f"{__name__}.{name}")
        except ModuleNotFoundError as exc:
            raise AttributeError(
                f"hermes/routes/{name}.py does not exist yet ({exc})."
            ) from exc
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
