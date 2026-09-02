"""ASGI entry point for hermes-core.

Run by the container as ``uvicorn hermes.main:app --host 0.0.0.0 --port 8080``.

Startup order matters and is deliberate:

1. **Logging first**, so anything the later steps log is actually formatted.
2. **``init_db()``** — creates the SQLite file, enables WAL, creates tables. If
   this fails the service is useless, so the exception is allowed to propagate
   and kill the container rather than serving an API backed by no database.
3. **Routers** — resolved through :func:`hermes.routes.load_routers`, which
   fails loudly and names the missing file rather than quietly serving a
   half-mounted API.
4. **A configuration banner** naming the freellmapi and MCP endpoints, and
   listing anything the user still has to set up. This is the first thing
   ``docker compose logs hermes-core`` shows, and it is where a missing
   ``FREELLMAPI_KEY`` gets caught — a 401 buried in a run's event log is much
   harder to diagnose.

Dependencies are deliberately *not* probed at startup. freellmapi and
linkedin-mcp are allowed to be down, unconfigured, or logged out; the dashboard
is supposed to show that state and walk the user through fixing it. A core that
refused to boot until LinkedIn was authenticated would make the very page that
explains how to authenticate unreachable.
"""

from __future__ import annotations

import logging
import os
import sys
from contextlib import asynccontextmanager
from typing import Any, AsyncIterator

from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from hermes import __version__
from hermes.db import init_db
from hermes.routes import RouteModuleError, load_routers
from hermes.settings import settings

log = logging.getLogger("hermes")

#: Paths that must never be logged with their query string (none carry secrets
#: today, but settings PUTs do carry values worth not echoing).
_QUIET_PATHS = ("/api/health",)


def _configure_logging() -> None:
    """One-line structured-ish logging to stdout, at the configured level."""
    level = getattr(logging, str(settings.log_level or "INFO").upper(), logging.INFO)
    root = logging.getLogger()
    # uvicorn installs its own handlers; add ours only once and let it coexist.
    if not any(getattr(h, "_hermes", False) for h in root.handlers):
        handler = logging.StreamHandler(sys.stdout)
        handler.setFormatter(
            logging.Formatter(
                "%(asctime)s %(levelname)-7s %(name)s: %(message)s",
                datefmt="%Y-%m-%d %H:%M:%S",
            )
        )
        handler._hermes = True  # type: ignore[attr-defined]
        root.addHandler(handler)
    root.setLevel(level)
    # SQLAlchemy's INFO tier echoes every statement; that is DEBUG-grade noise.
    logging.getLogger("sqlalchemy.engine").setLevel(max(level, logging.WARNING))


def _cors_origins() -> list[str]:
    """Dashboard origin(s) plus any operator-configured extras."""
    port = int(getattr(settings, "hermes_dashboard_port", 3000) or 3000)
    origins = {
        f"http://localhost:{port}",
        f"http://127.0.0.1:{port}",
        # Vite dev server, for running the UI outside the container.
        "http://localhost:5173",
        "http://127.0.0.1:5173",
    }
    extra = getattr(settings, "hermes_extra_cors_origins", "") or ""
    for item in str(extra).split(","):
        item = item.strip()
        if item:
            origins.add(item)
    return sorted(origins)


def _startup_banner() -> None:
    """Log what Hermes is pointed at and what the user still needs to do."""
    log.info("Hermes core %s starting", __version__)
    log.info("  LLM router (freellmapi) : %s", settings.freellmapi_base_url)
    log.info("  LinkedIn MCP            : %s", settings.linkedin_mcp_url)
    log.info("  data dir                : %s", settings.hermes_data_dir)
    log.info("  sandbox image           : %s", settings.hermes_sandbox_image)
    log.info(
        "  sandbox limits          : %s MB / %s CPU / %ss / network=%s",
        settings.hermes_sandbox_memory_mb,
        settings.hermes_sandbox_cpus,
        settings.hermes_sandbox_timeout_s,
        settings.hermes_sandbox_network,
    )
    log.info("  docker host             : %s", settings.hermes_docker_host)

    todo: list[str] = []
    if not str(settings.freellmapi_key or "").strip():
        todo.append(
            "FREELLMAPI_KEY is not set — open the freellmapi dashboard "
            f"(http://localhost:{settings.freellmapi_port}), create the local account, add at least "
            "one free provider key, copy the 'freellmapi-...' token into .env, then restart "
            "hermes-core. Until then every LLM call will fail with 401."
        )
    if not os.path.exists(settings.hermes_docker_host.replace("unix://", "")) and (
        settings.hermes_docker_host.startswith("unix://")
    ):
        todo.append(
            f"The Docker socket {settings.hermes_docker_host} is not present in this container — "
            "container management and the sandbox will report 503. Check the "
            "/var/run/docker.sock mount in docker-compose.yml."
        )
    for item in todo:
        log.warning("SETUP: %s", item)
    if not todo:
        log.info("  configuration           : complete")


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Create the schema, announce configuration, and drain runs on shutdown."""
    _configure_logging()
    init_db()
    _startup_banner()
    yield
    # Give in-flight runs a moment to record a terminal status so the dashboard
    # does not show a run stuck at "running" forever after a restart.
    try:
        from hermes.runner import shutdown_runs

        await shutdown_runs()
    except Exception as exc:  # pragma: no cover - best-effort teardown
        log.debug("run shutdown skipped: %s", exc)


def create_app() -> FastAPI:
    """Build the Hermes FastAPI application."""
    _configure_logging()

    app = FastAPI(
        title="Hermes",
        version=__version__,
        summary="Self-hosted LinkedIn analysis, ATS resume builder, and job scout.",
        description=(
            "Hermes analyses your LinkedIn profile, rewrites your resume for ATS "
            "parseability, scouts and ranks jobs, and hands you apply links. It does "
            "not submit applications on your behalf."
        ),
        lifespan=lifespan,
        docs_url="/api/docs",
        openapi_url="/api/openapi.json",
        redoc_url=None,
    )

    # CORS is only needed when the UI is served from a different origin than the
    # API (Vite dev server, or a LAN address). In the shipped stack nginx
    # proxies /api on the same origin, so this is a convenience, not a
    # requirement — hence a fixed allow-list rather than a wildcard.
    from fastapi.middleware.cors import CORSMiddleware

    app.add_middleware(
        CORSMiddleware,
        allow_origins=_cors_origins(),
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    try:
        mounted = load_routers()
    except RouteModuleError as exc:
        # Fail fast and say exactly which file is missing. A half-mounted API
        # produces 404s that look like frontend bugs.
        log.critical("Cannot mount the Hermes API: %s", exc)
        raise

    for slot, router in mounted:
        app.include_router(router, prefix="/api")
        log.debug("mounted route module: %s", slot)
    log.info("mounted %d route modules under /api", len(mounted))

    @app.exception_handler(RequestValidationError)
    async def _validation_handler(request: Request, exc: RequestValidationError) -> JSONResponse:
        """Return validation problems in the same envelope as other errors."""
        return JSONResponse(
            status_code=422,
            content={
                "error": "validation_error",
                "detail": "The request body or query parameters are invalid.",
                "problems": exc.errors(),
                "path": request.url.path,
            },
        )

    @app.exception_handler(HTTPException)
    async def _http_handler(request: Request, exc: HTTPException) -> JSONResponse:
        return JSONResponse(
            status_code=exc.status_code,
            content={
                "error": "http_error",
                "detail": exc.detail,
                "path": request.url.path,
            },
            headers=getattr(exc, "headers", None),
        )

    @app.exception_handler(Exception)
    async def _unhandled_handler(request: Request, exc: Exception) -> JSONResponse:
        """
        Never leak a raw traceback to the browser, but always log one.

        The dashboard shows `detail` verbatim, so it has to be a sentence a
        human can act on rather than a repr.
        """
        log.exception("unhandled error on %s %s", request.method, request.url.path)
        return JSONResponse(
            status_code=500,
            content={
                "error": "internal_error",
                "detail": (
                    f"{type(exc).__name__}: {exc}. See `docker compose logs hermes-core` "
                    "for the traceback."
                ),
                "path": request.url.path,
            },
        )

    @app.get("/", include_in_schema=False)
    async def _root() -> dict[str, Any]:
        """Friendly landing payload for anyone hitting the API port directly."""
        return {
            "service": "hermes-core",
            "version": __version__,
            "dashboard": f"http://localhost:{settings.hermes_dashboard_port}",
            "docs": "/api/docs",
            "health": "/api/health",
        }

    return app


app = create_app()
