"""Ephemeral Docker sandbox manager — the "container sandbox mode" heart of Hermes.

Two responsibilities live here:

1. **Agent code execution.** :meth:`SandboxManager.run_python` spawns a short-lived,
   heavily-restricted container from ``$HERMES_SANDBOX_IMAGE``, drops a per-run
   workspace in at ``/work``, runs the supplied Python source, then collects
   stdout/stderr/artifacts and destroys the container. Every hardening flag in the
   Hermes build contract is applied unconditionally (no network, memory + CPU +
   PID caps, all capabilities dropped, no-new-privileges, read-only root filesystem
   with a small tmpfs, unprivileged uid/gid 1000).

2. **Container management for the dashboard.** :meth:`SandboxManager.list_containers`,
   ``start``/``stop``/``restart``/``remove``, :meth:`stats`, :meth:`logs` and
   :meth:`logs_stream` back the ``/api/containers`` routes. These operate on *all*
   containers visible through the mounted Docker socket (so the dashboard can drive
   the whole compose project), and every returned record carries the raw label map,
   which includes the ``com.docker.compose.*`` labels the UI groups by.

The Docker SDK is entirely synchronous, so every blocking call is pushed onto a
worker thread with :func:`asyncio.to_thread`; nothing here may block the FastAPI
event loop.

Path translation note (important on Docker Desktop / Windows)
------------------------------------------------------------
hermes-core normally runs *inside* a container and talks to the host daemon over
``/var/run/docker.sock``. Bind-mount source paths in an API call are resolved by the
**daemon**, not by hermes-core's own filesystem — so we cannot naively bind
``/data/workspaces/...`` (a path that only exists inside hermes-core). We therefore
inspect our own container, find which mount contains the workspace root, and rewrite
the path to that mount's daemon-side ``Source`` (for a named volume this is the
volume's mountpoint, which is valid inside the Docker Desktop VM too). Overridable
with ``HERMES_SANDBOX_HOST_WORKSPACE``. When hermes-core runs directly on the host
(local development, including Windows) the path is used verbatim.
"""

from __future__ import annotations

import asyncio
import inspect
import json
import logging
import os
import posixpath
import shutil
import socket
import threading
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, AsyncIterator, Dict, Iterable, List, Optional

from .settings import settings

logger = logging.getLogger(__name__)

# --------------------------------------------------------------------------------------
# Optional docker SDK import.
#
# The SDK is a hard runtime requirement for sandbox execution, but sandbox.py must stay
# importable without it so that `GET /api/health` can report `docker: false` with a clear
# message instead of the whole API failing to boot.
# --------------------------------------------------------------------------------------
_DOCKER_IMPORT_ERROR: Optional[BaseException] = None
try:  # pragma: no cover - environment dependent
    import docker as _docker
    from docker.errors import APIError, DockerException, ImageNotFound, NotFound
except Exception as _exc:  # pragma: no cover - environment dependent
    _docker = None  # type: ignore[assignment]
    _DOCKER_IMPORT_ERROR = _exc

    class DockerException(Exception):  # type: ignore[no-redef]
        """Stand-in used when the docker SDK is not installed."""

    class APIError(DockerException):  # type: ignore[no-redef]
        """Stand-in used when the docker SDK is not installed."""

    class NotFound(DockerException):  # type: ignore[no-redef]
        """Stand-in used when the docker SDK is not installed."""

    class ImageNotFound(NotFound):  # type: ignore[no-redef]
        """Stand-in used when the docker SDK is not installed."""


# --------------------------------------------------------------------------------------
# Constants
# --------------------------------------------------------------------------------------

#: Label namespace applied to every container Hermes creates, so dashboard views and
#: cleanup routines can scope themselves to this project.
LABEL_PROJECT = "hermes.project"
LABEL_ROLE = "hermes.role"
LABEL_RUN = "hermes.run_id"
LABEL_SANDBOX = "hermes.sandbox_id"
PROJECT_LABEL_VALUE = "hermes"

#: Name of the entrypoint script written into every sandbox workspace.
ENTRY_SCRIPT = "main.py"

#: Mount point of the per-run workspace inside the sandbox container.
WORK_DIR = "/work"

#: Files/directories never reported as artifacts (bookkeeping + tool scratch).
_ARTIFACT_SKIP_NAMES = {"__pycache__", ENTRY_SCRIPT}

#: Environment handed to sandboxed processes. HOME/TMPDIR point at writable locations
#: because the root filesystem is read-only.
_BASE_ENV: Dict[str, str] = {
    "PYTHONUNBUFFERED": "1",
    "PYTHONDONTWRITEBYTECODE": "1",
    "PYTHONIOENCODING": "utf-8",
    "HOME": WORK_DIR,
    "TMPDIR": "/tmp",
    "XDG_CACHE_HOME": "/tmp/.cache",
    "XDG_CONFIG_HOME": "/tmp/.config",
    "XDG_RUNTIME_DIR": "/tmp/.runtime",
    "MPLCONFIGDIR": "/tmp/.mpl",
    "LC_ALL": "C.UTF-8",
    "LANG": "C.UTF-8",
}


class SandboxUnavailableError(RuntimeError):
    """Raised when the Docker daemon (or the sandbox image) cannot be used.

    The message is intentionally verbose and actionable: this is the error a user sees
    in the dashboard when the Docker socket was not mounted or the image was never built.
    """


# Backwards/forwards-compatible alias — other modules may catch either name.
DockerUnavailableError = SandboxUnavailableError


@dataclass
class SandboxResult:
    """Outcome of a single sandboxed execution."""

    exit_code: int
    stdout: str
    stderr: str
    artifacts: List[str]
    container_id: str

    #: Extra diagnostics (not part of the contract's required fields, but handy for the
    #: dashboard / Run.result_json). ``timed_out`` is True when we killed the container.
    timed_out: bool = False
    duration_s: float = 0.0
    workspace: str = ""
    limits: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        """JSON-serialisable view (used for ``Run.result_json``)."""
        return {
            "exit_code": self.exit_code,
            "stdout": self.stdout,
            "stderr": self.stderr,
            "artifacts": list(self.artifacts),
            "container_id": self.container_id,
            "timed_out": self.timed_out,
            "duration_s": round(self.duration_s, 3),
            "workspace": self.workspace,
            "limits": dict(self.limits),
        }

    @property
    def ok(self) -> bool:
        return self.exit_code == 0 and not self.timed_out


# --------------------------------------------------------------------------------------
# Small helpers that keep sandbox.py usable in isolation
# --------------------------------------------------------------------------------------


async def _emit(run_id: Optional[str], message: str, level: str = "info") -> None:
    """Publish a progress line on the event bus (best-effort).

    Imported lazily so this module stays importable (and unit-testable) without the
    rest of the package wired up, and so we never create an import cycle with
    ``events.py`` -> ``db.py`` -> ``models.py``.
    """
    if not run_id:
        logger.log(logging.WARNING if level in ("warn", "warning", "error") else logging.INFO, "sandbox: %s", message)
        return
    try:
        from .events import bus  # local import: see docstring

        result = bus.publish(run_id, level, message)
        if inspect.isawaitable(result):
            await result
    except Exception as exc:  # pragma: no cover - event bus is non-critical
        logger.debug("event bus publish failed (%s): %s", exc, message)


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _id_column_is_string() -> bool:
    """True when ``Sandbox.id`` is a string PK we are expected to populate ourselves."""
    try:
        from .models import Sandbox  # local import, see _emit docstring

        python_type = Sandbox.__table__.c.id.type.python_type
        return python_type is str
    except Exception:
        return False


def _record_sandbox_row(
    *,
    sandbox_id: str,
    container_id: str,
    image: str,
    run_id: Optional[str],
    status: str,
    limits: Dict[str, Any],
) -> Optional[Any]:
    """Insert a ``Sandbox`` row. Returns the primary key, or None on failure.

    Bookkeeping must never break execution, so every failure is swallowed and logged.
    """
    try:
        from .db import SessionLocal
        from .models import Sandbox

        kwargs: Dict[str, Any] = {
            "container_id": container_id,
            "image": image,
            "run_id": run_id,
            "status": status,
            "created_at": _utcnow(),
            "limits_json": json.dumps(limits),
        }
        if _id_column_is_string():
            kwargs["id"] = sandbox_id
        with SessionLocal() as session:
            row = Sandbox(**kwargs)
            session.add(row)
            session.commit()
            return row.id
    except Exception as exc:  # pragma: no cover - DB is optional for exec
        logger.warning("could not record Sandbox row: %s", exc)
        return None


def _finish_sandbox_row(pk: Any, *, status: str, exit_code: Optional[int]) -> None:
    """Mark a ``Sandbox`` row finished (best-effort)."""
    if pk is None:
        return
    try:
        from .db import SessionLocal
        from .models import Sandbox

        with SessionLocal() as session:
            row = session.get(Sandbox, pk)
            if row is None:
                return
            row.status = status
            row.exit_code = exit_code
            row.finished_at = _utcnow()
            session.commit()
    except Exception as exc:  # pragma: no cover
        logger.warning("could not finalise Sandbox row %s: %s", pk, exc)


def _safe_relpath(name: str) -> str:
    """Validate a caller-supplied workspace-relative filename.

    Blocks absolute paths, drive letters, and ``..`` traversal so a prompt-injected
    filename cannot write outside the per-run workspace.
    """
    cleaned = str(name).replace("\\", "/").strip()
    if not cleaned:
        raise ValueError("sandbox file name must not be empty")
    if cleaned.startswith("/") or (len(cleaned) > 1 and cleaned[1] == ":"):
        raise ValueError(f"sandbox file name must be relative, got {name!r}")
    normalised = posixpath.normpath(cleaned)
    if normalised.startswith("..") or normalised == ".":
        raise ValueError(f"sandbox file name escapes the workspace: {name!r}")
    return normalised


def _decode(blob: Any) -> str:
    if blob is None:
        return ""
    if isinstance(blob, bytes):
        return blob.decode("utf-8", errors="replace")
    return str(blob)


# --------------------------------------------------------------------------------------
# SandboxManager
# --------------------------------------------------------------------------------------


class SandboxManager:
    """Owns the Docker client, ephemeral execution, and container administration."""

    def __init__(
        self,
        docker_host: Optional[str] = None,
        image: Optional[str] = None,
        workspace_root: Optional[str] = None,
    ) -> None:
        self.docker_host = docker_host or settings.hermes_docker_host
        self.image = image or settings.hermes_sandbox_image
        self.workspace_root = Path(workspace_root or settings.hermes_sandbox_workspace)
        self._client: Any = None
        self._client_lock = threading.Lock()
        self._path_map_cache: Optional[List[Dict[str, str]]] = None

    # ---------------------------------------------------------------- client plumbing

    @property
    def client(self) -> Any:
        """Lazily-created :class:`docker.DockerClient`.

        Raises :class:`SandboxUnavailableError` with remediation steps when the daemon
        cannot be reached — this is by far the most common deployment mistake.
        """
        if self._client is not None:
            return self._client
        with self._client_lock:
            if self._client is not None:  # pragma: no cover - race
                return self._client
            if _docker is None:
                raise SandboxUnavailableError(
                    "The 'docker' Python SDK is not installed in hermes-core "
                    f"(import error: {_DOCKER_IMPORT_ERROR}). Add 'docker' to "
                    "services/core/requirements.txt and rebuild the image."
                )
            try:
                client = _docker.DockerClient(base_url=self.docker_host, timeout=120)
                client.ping()
            except Exception as exc:
                raise SandboxUnavailableError(
                    f"Cannot reach the Docker daemon at {self.docker_host!r}: {exc}. "
                    "Hermes needs the Docker socket to spawn sandbox containers. Check that "
                    "the compose service mounts it (volumes: - /var/run/docker.sock:/var/run/docker.sock), "
                    "that HERMES_DOCKER_HOST matches, and that Docker Desktop / dockerd is running."
                ) from exc
            self._client = client
            return client

    def docker_available(self) -> bool:
        """Cheap boolean for ``GET /api/health``. Never raises."""
        try:
            self.client.ping()
            return True
        except Exception:
            return False

    async def ping(self) -> Dict[str, Any]:
        """Async health probe with detail, image presence and daemon version."""

        def _probe() -> Dict[str, Any]:
            info: Dict[str, Any] = {
                "available": False,
                "docker_host": self.docker_host,
                "image": self.image,
                "image_present": False,
                "version": None,
                "detail": "",
            }
            try:
                client = self.client
                version = client.version()
                info["available"] = True
                info["version"] = version.get("Version")
                try:
                    client.images.get(self.image)
                    info["image_present"] = True
                    info["detail"] = "ok"
                except Exception:
                    info["detail"] = (
                        f"Docker is reachable but the sandbox image {self.image!r} is missing. "
                        "Build it with: docker compose --profile build-only build hermes-sandbox"
                    )
            except SandboxUnavailableError as exc:
                info["detail"] = str(exc)
            except Exception as exc:  # pragma: no cover
                info["detail"] = f"{type(exc).__name__}: {exc}"
            return info

        return await asyncio.to_thread(_probe)

    def limits(self, network: Optional[str] = None, timeout: Optional[int] = None) -> Dict[str, Any]:
        """Effective sandbox limits, preferring ``settings.sandbox_limits()``."""
        base: Dict[str, Any] = {}
        helper = getattr(settings, "sandbox_limits", None)
        if callable(helper):
            try:
                base = dict(helper() or {})
            except Exception:  # pragma: no cover - defensive
                base = {}
        merged = {
            "image": base.get("image", self.image),
            "memory_mb": int(base.get("memory_mb", settings.hermes_sandbox_memory_mb)),
            "cpus": float(base.get("cpus", settings.hermes_sandbox_cpus)),
            "timeout_s": int(timeout if timeout is not None else base.get("timeout_s", settings.hermes_sandbox_timeout_s)),
            "network": network if network is not None else base.get("network", settings.hermes_sandbox_network),
            "pids_limit": 256,
            "read_only_rootfs": True,
            "cap_drop": ["ALL"],
            "no_new_privileges": True,
            "user": "1000:1000",
            "tmpfs": {"/tmp": "size=64m"},
        }
        return merged

    # ------------------------------------------------------------- path translation

    def _mount_table(self) -> List[Dict[str, str]]:
        """Destination -> daemon-side Source mappings for hermes-core's own container.

        Empty list when we are not running inside a container (then paths are already
        daemon/host paths).
        """
        if self._path_map_cache is not None:
            return self._path_map_cache

        table: List[Dict[str, str]] = []

        # 1) Explicit override wins: HERMES_SANDBOX_HOST_WORKSPACE is the host/daemon path
        #    that corresponds to HERMES_SANDBOX_WORKSPACE inside hermes-core.
        override = os.getenv("HERMES_SANDBOX_HOST_WORKSPACE", "").strip()
        if override:
            table.append({"destination": self.workspace_root.as_posix(), "source": override})

        # 2) Otherwise discover it by inspecting our own container.
        candidates = [os.getenv("HERMES_CORE_CONTAINER_ID", "").strip(), socket.gethostname()]
        for cid in candidates:
            if not cid:
                continue
            try:
                container = self.client.containers.get(cid)
            except Exception:
                continue
            for mount in container.attrs.get("Mounts") or []:
                dest = mount.get("Destination")
                source = mount.get("Source")
                if dest and source:
                    table.append({"destination": dest, "source": source})
            break

        # Longest destination first so the most specific mount wins.
        table.sort(key=lambda m: len(m["destination"]), reverse=True)
        self._path_map_cache = table
        return table

    def _to_daemon_path(self, container_path: Path) -> str:
        """Rewrite a hermes-core-local path into a path the Docker daemon can bind."""
        as_posix = container_path.as_posix()
        for mount in self._mount_table():
            dest = mount["destination"].rstrip("/") or "/"
            if as_posix == dest or as_posix.startswith(dest + "/"):
                remainder = as_posix[len(dest) :].lstrip("/")
                source = mount["source"].rstrip("/")
                # Preserve the daemon's separator style: Windows host paths keep backslashes.
                if "\\" in source and ":" in source[:3]:
                    return source + ("\\" + remainder.replace("/", "\\") if remainder else "")
                return source + ("/" + remainder if remainder else "")
        # Not containerised (or unmapped): use the path verbatim. On Windows this is the
        # native path, which Docker Desktop accepts as a bind source.
        return str(container_path) if os.name == "nt" else as_posix

    # ------------------------------------------------------------------- execution

    async def run_python(
        self,
        code: str,
        files: Optional[Dict[str, str]] = None,
        timeout: Optional[int] = None,
        run_id: Optional[str] = None,
        network: Optional[str] = None,
        *,
        argv: Optional[List[str]] = None,
        extra_env: Optional[Dict[str, str]] = None,
        keep_container: bool = False,
    ) -> SandboxResult:
        """Execute ``code`` inside a fresh, hardened, ephemeral container.

        Args:
            code: Python source written to ``/work/main.py`` and run with ``python -u``.
            files: Extra workspace files as ``{relative_path: text}`` (e.g. a helper
                script such as ``ats_docx.py``, or input documents).
            timeout: Wall-clock seconds before the container is **killed**. Defaults to
                ``HERMES_SANDBOX_TIMEOUT_S``.
            run_id: Run to stream progress events to, and to group the workspace under.
            network: Docker network, or ``"none"`` for a fully network-disabled container.
                Defaults to ``HERMES_SANDBOX_NETWORK``.
            argv: Extra argv passed after the script path.
            extra_env: Additional environment variables.
            keep_container: Leave the container on disk for debugging (also honoured via
                ``HERMES_SANDBOX_KEEP=1``).

        Returns:
            :class:`SandboxResult` with exit code, captured streams, and the absolute
            (hermes-core-local) paths of every artifact left in the workspace.

        Raises:
            SandboxUnavailableError: Docker unreachable, or the sandbox image is missing.
        """
        lim = self.limits(network=network, timeout=timeout)
        effective_timeout = int(lim["timeout_s"])
        effective_network = str(lim["network"] or "none")
        sandbox_id = uuid.uuid4().hex[:12]
        keep = keep_container or os.getenv("HERMES_SANDBOX_KEEP", "").strip() in ("1", "true", "yes")

        # --- workspace ------------------------------------------------------------
        workspace = self.workspace_root / (run_id or "adhoc") / sandbox_id
        injected: List[str] = []
        try:
            workspace.mkdir(parents=True, exist_ok=True)
            # The sandbox process runs as uid 1000; make sure it can write here. chmod is a
            # no-op on Windows, hence the guard (Hermes must not rely on it).
            try:
                os.chmod(workspace, 0o777)
            except Exception:
                pass
            (workspace / ENTRY_SCRIPT).write_text(code, encoding="utf-8")
            for name, content in (files or {}).items():
                rel = _safe_relpath(name)
                target = workspace / rel
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(content, encoding="utf-8")
                injected.append(rel)
        except OSError as exc:
            raise SandboxUnavailableError(
                f"Cannot prepare the sandbox workspace at {workspace}: {exc}. "
                f"Check that HERMES_SANDBOX_WORKSPACE ({self.workspace_root}) exists and is writable "
                "inside hermes-core (it normally lives on the /data volume)."
            ) from exc

        # --- verify the image exists before we promise anything --------------------
        client = self.client  # may raise SandboxUnavailableError
        try:
            await asyncio.to_thread(client.images.get, self.image)
        except NotFound as exc:
            raise SandboxUnavailableError(
                f"Sandbox image {self.image!r} not found. Build it once with:\n"
                "    docker compose --profile build-only build hermes-sandbox\n"
                "(or set HERMES_SANDBOX_IMAGE to an image that already exists)."
            ) from exc
        except SandboxUnavailableError:
            raise
        except Exception as exc:  # pragma: no cover - transport errors
            raise SandboxUnavailableError(f"Could not inspect sandbox image {self.image!r}: {exc}") from exc

        env = dict(_BASE_ENV)
        env.update(extra_env or {})
        command = ["python", "-u", f"{WORK_DIR}/{ENTRY_SCRIPT}"] + [str(a) for a in (argv or [])]

        create_kwargs: Dict[str, Any] = dict(
            image=self.image,
            command=command,
            name=f"hermes-sandbox-{sandbox_id}",
            detach=True,
            # --- mandatory hardening (build contract) ---
            user="1000:1000",
            mem_limit=f"{int(lim['memory_mb'])}m",
            nano_cpus=int(float(lim["cpus"]) * 1_000_000_000),
            pids_limit=256,
            cap_drop=["ALL"],
            security_opt=["no-new-privileges:true"],
            read_only=True,
            tmpfs={"/tmp": "size=64m"},
            auto_remove=False,
            # --------------------------------------------
            working_dir=WORK_DIR,
            environment=env,
            volumes={self._to_daemon_path(workspace): {"bind": WORK_DIR, "mode": "rw"}},
            labels={
                LABEL_PROJECT: PROJECT_LABEL_VALUE,
                LABEL_ROLE: "sandbox",
                LABEL_RUN: run_id or "",
                LABEL_SANDBOX: sandbox_id,
            },
            # Belt-and-braces resource caps.
            mem_swappiness=0,
            oom_kill_disable=False,
        )
        if effective_network == "none":
            create_kwargs["network_disabled"] = True
        else:
            create_kwargs["network"] = effective_network

        await _emit(
            run_id,
            f"sandbox {sandbox_id}: starting {self.image} "
            f"(mem={lim['memory_mb']}MB cpus={lim['cpus']} net={effective_network} timeout={effective_timeout}s)",
        )

        started = time.monotonic()
        container = None
        row_pk = None
        timed_out = False
        exit_code = -1
        try:
            try:
                container = await asyncio.to_thread(lambda: client.containers.run(**create_kwargs))
            except APIError as exc:
                raise SandboxUnavailableError(
                    f"Docker refused to start the sandbox container: {exc}. "
                    "This usually means the image is broken, the workspace bind source does not exist on the "
                    "daemon host (see HERMES_SANDBOX_HOST_WORKSPACE), or the daemon does not support one of the "
                    "hardening options."
                ) from exc

            container_id = getattr(container, "id", "") or ""
            row_pk = await asyncio.to_thread(
                _record_sandbox_row,
                sandbox_id=sandbox_id,
                container_id=container_id,
                image=self.image,
                run_id=run_id,
                status="running",
                limits=lim,
            )
            await _emit(run_id, f"sandbox {sandbox_id}: container {container_id[:12]} running")

            exit_code, timed_out = await self._wait_or_kill(container, effective_timeout, run_id, sandbox_id)

            stdout = _decode(await asyncio.to_thread(lambda: container.logs(stdout=True, stderr=False)))
            stderr = _decode(await asyncio.to_thread(lambda: container.logs(stdout=False, stderr=True)))
            if timed_out:
                stderr = (
                    stderr.rstrip()
                    + f"\n[hermes] sandbox timed out after {effective_timeout}s and was killed.\n"
                ).lstrip("\n")

            artifacts = await asyncio.to_thread(self._collect_artifacts, workspace, injected)
            duration = time.monotonic() - started

            status = "timeout" if timed_out else ("done" if exit_code == 0 else "error")
            await asyncio.to_thread(_finish_sandbox_row, row_pk, status=status, exit_code=exit_code)
            await _emit(
                run_id,
                f"sandbox {sandbox_id}: finished status={status} exit={exit_code} "
                f"in {duration:.1f}s, {len(artifacts)} artifact(s)",
                level="error" if status == "error" else "info",
            )

            return SandboxResult(
                exit_code=exit_code,
                stdout=stdout,
                stderr=stderr,
                artifacts=artifacts,
                container_id=container_id,
                timed_out=timed_out,
                duration_s=duration,
                workspace=str(workspace),
                limits=lim,
            )
        except SandboxUnavailableError:
            await asyncio.to_thread(_finish_sandbox_row, row_pk, status="error", exit_code=None)
            raise
        except Exception as exc:
            await asyncio.to_thread(_finish_sandbox_row, row_pk, status="error", exit_code=None)
            await _emit(run_id, f"sandbox {sandbox_id}: failed: {type(exc).__name__}: {exc}", level="error")
            raise
        finally:
            if container is not None and not keep:
                try:
                    await asyncio.to_thread(lambda: container.remove(force=True, v=True))
                except Exception as exc:  # pragma: no cover
                    logger.debug("could not remove sandbox container: %s", exc)

    async def _wait_or_kill(
        self,
        container: Any,
        timeout: int,
        run_id: Optional[str],
        sandbox_id: str,
    ) -> tuple:
        """Poll until the container exits; kill it once ``timeout`` elapses.

        Polling (rather than ``container.wait(timeout=...)``) avoids depending on how the
        installed urllib3/requests version surfaces read timeouts, and guarantees the kill
        actually happens — a cancelled ``asyncio.to_thread`` would otherwise leave the
        blocking wait running in a worker thread.
        """
        deadline = time.monotonic() + max(1, int(timeout))
        poll = 0.2
        while True:
            state = await asyncio.to_thread(self._container_state, container)
            status = state.get("Status") or ""
            if status not in ("created", "running", "restarting", "paused", ""):
                code = state.get("ExitCode")
                return (int(code) if isinstance(code, int) else -1), False
            if status == "created" and state.get("FinishedAt", "").startswith("0001"):
                # Freshly created; keep waiting.
                pass
            if time.monotonic() >= deadline:
                await _emit(run_id, f"sandbox {sandbox_id}: timeout reached, killing container", level="warning")
                try:
                    await asyncio.to_thread(container.kill)
                except Exception as exc:
                    logger.debug("kill failed, trying stop: %s", exc)
                    try:
                        await asyncio.to_thread(lambda: container.stop(timeout=1))
                    except Exception:  # pragma: no cover
                        pass
                # Give the daemon a moment to reap, then read the (killed) exit code.
                for _ in range(25):
                    await asyncio.sleep(0.2)
                    state = await asyncio.to_thread(self._container_state, container)
                    if (state.get("Status") or "") not in ("running", "restarting", "paused"):
                        break
                code = state.get("ExitCode")
                return (int(code) if isinstance(code, int) else 137), True
            await asyncio.sleep(poll)
            poll = min(poll * 1.25, 1.0)

    @staticmethod
    def _container_state(container: Any) -> Dict[str, Any]:
        try:
            container.reload()
        except Exception as exc:  # pragma: no cover - container vanished
            logger.debug("reload failed: %s", exc)
            return {"Status": "removed", "ExitCode": -1}
        return dict((container.attrs or {}).get("State") or {})

    @staticmethod
    def _collect_artifacts(workspace: Path, injected: Iterable[str]) -> List[str]:
        """Absolute paths of files the sandbox produced in its workspace.

        Injected inputs, the entrypoint script, and dotfile/scratch directories (e.g. the
        LibreOffice profile) are excluded so callers see only real outputs.
        """
        skip = {_safe_relpath(name) for name in injected}
        found: List[str] = []
        for path in sorted(workspace.rglob("*")):
            if not path.is_file():
                continue
            rel = path.relative_to(workspace).as_posix()
            parts = rel.split("/")
            if any(part.startswith(".") for part in parts):
                continue
            if any(part in _ARTIFACT_SKIP_NAMES for part in parts):
                continue
            if rel in skip:
                continue
            found.append(str(path))
        return found

    # -------------------------------------------------- container mgmt (dashboard)

    def _get_container(self, container_id: str) -> Any:
        try:
            return self.client.containers.get(container_id)
        except NotFound as exc:
            raise SandboxUnavailableError(f"No such container: {container_id}") from exc

    @staticmethod
    def _format_ports(attrs: Dict[str, Any]) -> List[str]:
        """Human-readable port bindings, e.g. ``127.0.0.1:3000->3000/tcp``."""
        out: List[str] = []
        ports = ((attrs.get("NetworkSettings") or {}).get("Ports")) or {}
        for container_port, bindings in sorted(ports.items()):
            if not bindings:
                out.append(container_port)
                continue
            for binding in bindings:
                host_ip = binding.get("HostIp") or "0.0.0.0"
                host_port = binding.get("HostPort") or ""
                out.append(f"{host_ip}:{host_port}->{container_port}")
        return out

    def _container_dict(self, container: Any) -> Dict[str, Any]:
        attrs = container.attrs or {}
        config = attrs.get("Config") or {}
        state = attrs.get("State") or {}
        image_name = ""
        try:
            tags = getattr(container.image, "tags", None) or []
            image_name = tags[0] if tags else (config.get("Image") or "")
        except Exception:
            image_name = config.get("Image") or ""
        return {
            "id": container.id,
            "short_id": (container.id or "")[:12],
            "name": (container.name or "").lstrip("/"),
            "image": image_name,
            "status": container.status,  # e.g. "running", "exited"
            "state": state.get("Status") or container.status,
            "health": ((state.get("Health") or {}).get("Status")),
            "ports": self._format_ports(attrs),
            "labels": dict(config.get("Labels") or {}),
            "created": attrs.get("Created"),
            "started_at": state.get("StartedAt"),
            "finished_at": state.get("FinishedAt"),
            "exit_code": state.get("ExitCode"),
            "command": " ".join(config.get("Cmd") or []) if config.get("Cmd") else "",
            "compose_project": (config.get("Labels") or {}).get("com.docker.compose.project"),
            "compose_service": (config.get("Labels") or {}).get("com.docker.compose.service"),
            "is_hermes": (config.get("Labels") or {}).get(LABEL_PROJECT) == PROJECT_LABEL_VALUE
            or (config.get("Labels") or {}).get("com.docker.compose.project", "").startswith("hermes"),
        }

    def list_containers(self, all_containers: bool = True) -> List[Dict[str, Any]]:
        """All containers visible to the daemon, newest first.

        Deliberately *not* label-filtered: the dashboard's Containers page manages the
        whole compose project (freellmapi, linkedin-mcp, hermes-core, hermes-dashboard)
        plus any ephemeral sandboxes. Each record exposes ``labels`` (including the
        ``com.docker.compose.*`` set) and ``is_hermes`` so the UI can group or filter.
        """
        containers = self.client.containers.list(all=all_containers)
        out = [self._container_dict(c) for c in containers]
        out.sort(key=lambda d: (d.get("created") or ""), reverse=True)
        return out

    def start(self, container_id: str) -> Dict[str, Any]:
        container = self._get_container(container_id)
        container.start()
        container.reload()
        return self._container_dict(container)

    def stop(self, container_id: str, timeout: int = 10) -> Dict[str, Any]:
        container = self._get_container(container_id)
        container.stop(timeout=timeout)
        container.reload()
        return self._container_dict(container)

    def restart(self, container_id: str, timeout: int = 10) -> Dict[str, Any]:
        container = self._get_container(container_id)
        container.restart(timeout=timeout)
        container.reload()
        return self._container_dict(container)

    def remove(self, container_id: str, force: bool = False) -> Dict[str, Any]:
        """Remove a container. Anonymous volumes are removed with it; named volumes stay.

        Named volumes (``linkedin-session``, the data volume) are intentionally preserved
        so removing a container never destroys the LinkedIn login session.
        """
        container = self._get_container(container_id)
        name = (container.name or "").lstrip("/")
        container.remove(force=force, v=False)
        return {"id": container_id, "name": name, "removed": True, "forced": bool(force)}

    def stats(self, container_id: str) -> Dict[str, Any]:
        """One-shot resource snapshot for the dashboard table."""
        container = self._get_container(container_id)
        raw = container.stats(stream=False)

        cpu_percent = 0.0
        try:
            cpu = raw.get("cpu_stats") or {}
            pre = raw.get("precpu_stats") or {}
            cpu_delta = (cpu.get("cpu_usage") or {}).get("total_usage", 0) - (pre.get("cpu_usage") or {}).get(
                "total_usage", 0
            )
            system_delta = cpu.get("system_cpu_usage", 0) - pre.get("system_cpu_usage", 0)
            online = cpu.get("online_cpus") or len((cpu.get("cpu_usage") or {}).get("percpu_usage") or []) or 1
            if cpu_delta > 0 and system_delta > 0:
                cpu_percent = (cpu_delta / system_delta) * float(online) * 100.0
        except Exception:  # pragma: no cover - shape varies by platform
            cpu_percent = 0.0

        mem = raw.get("memory_stats") or {}
        usage = float(mem.get("usage") or 0.0)
        # Page cache is charged to the cgroup but is reclaimable; subtract it like `docker stats`.
        inactive_file = float(((mem.get("stats") or {}).get("inactive_file")) or ((mem.get("stats") or {}).get("total_inactive_file")) or 0.0)
        usage = max(usage - inactive_file, 0.0)
        limit = float(mem.get("limit") or 0.0)

        rx = tx = 0
        for iface in (raw.get("networks") or {}).values():
            rx += int(iface.get("rx_bytes") or 0)
            tx += int(iface.get("tx_bytes") or 0)

        return {
            "id": container.id,
            "name": (container.name or "").lstrip("/"),
            "cpu_percent": round(cpu_percent, 2),
            "mem_usage_mb": round(usage / (1024 * 1024), 2),
            "mem_limit_mb": round(limit / (1024 * 1024), 2),
            "mem_percent": round((usage / limit * 100.0), 2) if limit else 0.0,
            "net_rx": rx,
            "net_tx": tx,
            "pids": int(((raw.get("pids_stats") or {}).get("current")) or 0),
            "read_at": _utcnow().isoformat(),
        }

    def logs(self, container_id: str, tail: int = 200) -> str:
        """Recent combined stdout/stderr as text."""
        container = self._get_container(container_id)
        return _decode(container.logs(stdout=True, stderr=True, tail=tail, timestamps=False))

    async def logs_stream(self, container_id: str, tail: int = 100) -> AsyncIterator[str]:
        """Async generator yielding decoded log lines, following the container.

        Backs ``GET /api/containers/{id}/logs`` (SSE). The docker SDK's follow mode is a
        blocking generator, so it is pumped from a daemon thread into an
        :class:`asyncio.Queue`; lines are dropped (with a marker) rather than blocking the
        pump if a slow client lets the queue fill.
        """
        loop = asyncio.get_running_loop()
        queue: "asyncio.Queue[Optional[str]]" = asyncio.Queue(maxsize=2000)
        container = await asyncio.to_thread(self._get_container, container_id)
        stop = threading.Event()

        def offer(line: Optional[str]) -> None:
            try:
                queue.put_nowait(line)
            except asyncio.QueueFull:  # pragma: no cover - slow consumer
                pass

        def pump() -> None:
            try:
                stream = container.logs(stdout=True, stderr=True, stream=True, follow=True, tail=tail)
                buffer = ""
                for chunk in stream:
                    if stop.is_set():
                        break
                    buffer += _decode(chunk)
                    *lines, buffer = buffer.split("\n")
                    for line in lines:
                        loop.call_soon_threadsafe(offer, line.rstrip("\r"))
                if buffer and not stop.is_set():
                    loop.call_soon_threadsafe(offer, buffer.rstrip("\r"))
            except Exception as exc:  # pragma: no cover - stream closed/container gone
                loop.call_soon_threadsafe(offer, f"[hermes] log stream ended: {type(exc).__name__}: {exc}")
            finally:
                loop.call_soon_threadsafe(offer, None)

        thread = threading.Thread(target=pump, name=f"hermes-logs-{container_id[:12]}", daemon=True)
        thread.start()
        try:
            while True:
                item = await queue.get()
                if item is None:
                    break
                yield item
        finally:
            stop.set()

    # ----------------------------------------------------------------- maintenance

    async def cleanup_workspace(self, run_id: str) -> int:
        """Delete every workspace directory belonging to ``run_id``. Returns bytes freed."""

        def _rm() -> int:
            target = self.workspace_root / run_id
            if not target.exists():
                return 0
            freed = sum(p.stat().st_size for p in target.rglob("*") if p.is_file())
            shutil.rmtree(target, ignore_errors=True)
            return freed

        return await asyncio.to_thread(_rm)

    async def prune_sandbox_containers(self) -> int:
        """Remove leftover exited Hermes sandbox containers. Returns the count removed."""

        def _prune() -> int:
            removed = 0
            try:
                containers = self.client.containers.list(
                    all=True, filters={"label": f"{LABEL_ROLE}=sandbox", "status": "exited"}
                )
            except Exception as exc:  # pragma: no cover
                logger.debug("prune list failed: %s", exc)
                return 0
            for container in containers:
                try:
                    container.remove(force=True, v=True)
                    removed += 1
                except Exception:  # pragma: no cover
                    continue
            return removed

        return await asyncio.to_thread(_prune)


# --------------------------------------------------------------------------------------
# Module factory
# --------------------------------------------------------------------------------------

_manager: Optional[SandboxManager] = None
_manager_lock = threading.Lock()


def get_sandbox() -> SandboxManager:
    """Return the process-wide :class:`SandboxManager` singleton."""
    global _manager
    if _manager is None:
        with _manager_lock:
            if _manager is None:
                _manager = SandboxManager()
    return _manager


__all__ = [
    "SandboxManager",
    "SandboxResult",
    "SandboxUnavailableError",
    "DockerUnavailableError",
    "get_sandbox",
    "LABEL_PROJECT",
    "LABEL_ROLE",
    "LABEL_RUN",
    "LABEL_SANDBOX",
    "WORK_DIR",
]
