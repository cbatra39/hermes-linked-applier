"""Database engine / session plumbing.

WHY SYNCHRONOUS SQLITE (deliberate, per build contract):
    Hermes is a single-user, self-hosted app whose real latency is LLM calls and
    LinkedIn scraping, not SQL. The SYNC `sqlite3` driver plus WAL gives us
    concurrent readers with a writer, zero extra dependency (`aiosqlite`), and —
    critically — it works from the *threadpool* that FastAPI already uses for
    sync dependencies, and from `asyncio.to_thread` inside the async pipelines.
    An async driver would force every model touch into `await`, for no gain.

    Rules for other modules:
      * In a route, take `db: Session = Depends(get_db)` — FastAPI runs sync
        dependencies in a worker thread, so blocking there is fine.
      * In an async pipeline/agent, never hold a Session across an `await`.
        Use `with session_scope() as db:` for a short unit of work, or
        `await in_thread(fn, ...)` to run a whole blocking DB function off-loop.

CONCURRENCY NOTES:
    * `check_same_thread=False` is required because sessions move between the
      threadpool workers.
    * WAL + `busy_timeout` (10s) makes the "database is locked" error effectively
      impossible for our write volume.
    * `foreign_keys=ON` must be set per-connection in SQLite (it is OFF by
      default), otherwise the ondelete=CASCADE declarations are silently inert.
"""

from __future__ import annotations

import asyncio
import logging
import sqlite3
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from typing import Any, TypeVar

from sqlalchemy import event, create_engine, text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from hermes.settings import settings

log = logging.getLogger("hermes.db")

T = TypeVar("T")

# Create the data directory (and resumes/uploads/workspaces) before SQLAlchemy
# tries to open the file — sqlite will not create missing parent directories.
settings.ensure_dirs()

DB_URL: str = settings.db_url


class Base(DeclarativeBase):
    """Declarative base for every Hermes model (see hermes/models.py)."""


engine: Engine = create_engine(
    DB_URL,
    echo=False,
    future=True,
    # A single SQLite file: keep a small pool, allow cross-thread use.
    connect_args={
        "check_same_thread": False,
        "timeout": 30,  # seconds the driver waits on a locked db
    },
    pool_pre_ping=True,
)

SessionLocal = sessionmaker(
    bind=engine,
    class_=Session,
    autoflush=False,
    autocommit=False,
    # Objects stay usable after commit() — routes serialise rows post-commit.
    expire_on_commit=False,
    future=True,
)


# --------------------------------------------------------------------------- #
# per-connection PRAGMAs
# --------------------------------------------------------------------------- #
@event.listens_for(engine, "connect")
def _sqlite_on_connect(dbapi_connection: Any, _connection_record: Any) -> None:
    """Enable WAL, FK enforcement and sane durability on every new connection.

    Guarded by an isinstance check so swapping the URL to Postgres later does
    not blow up here.
    """
    if not isinstance(dbapi_connection, sqlite3.Connection):
        return
    cursor = dbapi_connection.cursor()
    try:
        cursor.execute("PRAGMA journal_mode=WAL")  # concurrent readers + 1 writer
        cursor.execute("PRAGMA foreign_keys=ON")  # OFF by default in SQLite!
        cursor.execute("PRAGMA synchronous=NORMAL")  # safe with WAL, much faster
        cursor.execute("PRAGMA busy_timeout=10000")  # 10s instead of instant lock error
        cursor.execute("PRAGMA temp_store=MEMORY")
    except sqlite3.Error as exc:  # pragma: no cover - diagnostics only
        log.warning("Failed to apply SQLite PRAGMAs: %s", exc)
    finally:
        cursor.close()


# --------------------------------------------------------------------------- #
# session helpers
# --------------------------------------------------------------------------- #
def get_db() -> Iterator[Session]:
    """FastAPI dependency: yields a Session, always closed.

    Usage::

        @router.get("/jobs")
        def list_jobs(db: Session = Depends(get_db)): ...

    Commits are explicit — a read-only route should not pay for a commit, and a
    write route should decide its own transaction boundary.
    """
    db = SessionLocal()
    try:
        yield db
    except Exception:
        # Leave the DB clean if the route raised mid-transaction.
        db.rollback()
        raise
    finally:
        db.close()


@contextmanager
def session_scope() -> Iterator[Session]:
    """Transactional scope for background work: commits, rolls back, closes.

    Keep the body short and never `await` inside it (see module docstring).
    """
    db = SessionLocal()
    try:
        yield db
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


async def in_thread(fn: Callable[..., T], /, *args: Any, **kwargs: Any) -> T:
    """Run a blocking DB function in the default threadpool.

    Lets async pipelines do real DB work without stalling the event loop::

        row = await in_thread(_load_job, job_id)
    """
    return await asyncio.to_thread(fn, *args, **kwargs)


# --------------------------------------------------------------------------- #
# schema creation / bootstrap
# --------------------------------------------------------------------------- #
def init_db() -> None:
    """Create the schema (idempotent) and seed dashboard-editable settings.

    Imported models must be registered on `Base.metadata` before create_all, so
    `hermes.models` is imported here rather than at module scope (models.py
    imports Base from this module — importing it at the top would be circular).
    """
    from hermes import models  # noqa: F401  (registers mappers as a side effect)

    Base.metadata.create_all(bind=engine)
    _seed_settings()
    log.info("SQLite ready at %s (WAL, foreign_keys=ON)", settings.db_path)


def default_settings() -> dict[str, str]:
    """Dashboard-editable defaults, derived from the environment.

    Secrets are NOT stored here — FREELLMAPI_KEY / ENCRYPTION_KEY stay in env.
    """
    return {
        "model_primary": settings.hermes_model_primary or "",
        "model_fallbacks": settings.hermes_model_fallbacks or "",
        "job_search_keywords": "",
        "job_search_location": "",
        "job_search_easy_apply": "false",
        "job_search_max_pages": "3",
        "job_min_score": "60",
        "resume_target_pages": "1",
        "llm_temperature": "0.2",
    }


def _seed_settings() -> None:
    """Insert any missing default Setting rows without clobbering user edits."""
    from hermes.models import Setting

    with session_scope() as db:
        existing = {row.key for row in db.query(Setting).all()}
        for key, value in default_settings().items():
            if key not in existing:
                db.add(Setting(key=key, value=value))


def get_setting(db: Session, key: str, default: str | None = None) -> str | None:
    """Read one Setting value (None/default when absent)."""
    from hermes.models import Setting

    row = db.get(Setting, key)
    if row is None or row.value is None:
        return default
    return row.value


def set_setting(db: Session, key: str, value: str | None) -> None:
    """Upsert one Setting value. Caller commits."""
    from hermes.models import Setting, utcnow

    row = db.get(Setting, key)
    if row is None:
        db.add(Setting(key=key, value=value))
    else:
        row.value = value
        row.updated_at = utcnow()


def healthcheck() -> dict[str, Any]:
    """Cheap DB probe for GET /api/health."""
    try:
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
            mode = conn.execute(text("PRAGMA journal_mode")).scalar_one()
        return {"ok": True, "path": str(settings.db_path), "journal_mode": str(mode)}
    except Exception as exc:  # noqa: BLE001 - health must never raise
        return {"ok": False, "path": str(settings.db_path), "error": str(exc)}


__all__ = [
    "Base",
    "DB_URL",
    "SessionLocal",
    "default_settings",
    "engine",
    "get_db",
    "get_setting",
    "healthcheck",
    "in_thread",
    "init_db",
    "session_scope",
    "set_setting",
]
