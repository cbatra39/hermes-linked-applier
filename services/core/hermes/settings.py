"""Configuration for hermes-core.

Every value comes from the environment (docker-compose injects the single root
`.env`). Field names are the lowercase form of the env var, so `HERMES_DATA_DIR`
-> `settings.hermes_data_dir`. There is no env_prefix.

Import contract for other modules:
    from hermes.settings import settings          # module-level singleton
    settings.sandbox_limits()                     # docker hardening kwargs source
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path
from typing import Any

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

log = logging.getLogger("hermes.settings")


class ConfigError(RuntimeError):
    """Raised when a required piece of configuration is missing or nonsensical.

    Deliberately loud: a blank FREELLMAPI_KEY should surface as an obvious
    500/health failure with a fixable message, not as a mysterious 401 from the
    router three call frames deep.
    """


# Sandbox hard limits that are NOT operator-tunable (security invariants).
SANDBOX_PIDS_LIMIT = 256
SANDBOX_CAP_DROP = ["ALL"]
SANDBOX_SECURITY_OPT = ["no-new-privileges:true"]
SANDBOX_USER = "1000:1000"
SANDBOX_TMPFS = {"/tmp": "size=64m"}
SANDBOX_WORK_MOUNT = "/work"  # per-run workspace bind-mount point inside sandbox

# Label applied to every container Hermes creates, so the dashboard can scope
# its container list to this project instead of showing the whole host.
PROJECT_LABEL = "hermes.project"
PROJECT_LABEL_VALUE = "hermes"
ROLE_LABEL = "hermes.role"


class Settings(BaseSettings):
    """Typed view over the Hermes environment."""

    model_config = SettingsConfigDict(
        env_file=(".env",),  # only used for bare-metal dev; compose injects real env
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # --- networking / binding ----------------------------------------------
    host_bind: str = "127.0.0.1"

    # --- freellmapi (OpenAI-compatible free-tier router) -------------------
    encryption_key: str = ""  # 64-char hex, consumed by the freellmapi container
    freellmapi_base_url: str = "http://freellmapi:3001/v1"
    freellmapi_key: str = ""  # `freellmapi-...` client token minted in its dashboard
    hermes_model_primary: str = ""  # blank => auto-pick from GET /v1/models
    hermes_model_fallbacks: str = ""  # comma-separated model ids

    # --- linkedin-mcp -------------------------------------------------------
    linkedin_mcp_url: str = "http://linkedin-mcp:8000/mcp"

    # --- ports --------------------------------------------------------------
    hermes_api_port: int = 8080
    hermes_dashboard_port: int = 3000
    freellmapi_port: int = 3001
    linkedin_viewer_port: int = 6080

    # --- sandbox ------------------------------------------------------------
    hermes_sandbox_image: str = "hermes-linkedin/sandbox:latest"
    hermes_sandbox_memory_mb: int = 1024
    hermes_sandbox_cpus: float = 1.0
    hermes_sandbox_timeout_s: int = 300
    hermes_sandbox_network: str = "none"
    hermes_sandbox_workspace: str = "/data/workspaces"

    # --- storage / docker / logging ----------------------------------------
    hermes_data_dir: str = "/data"
    hermes_docker_host: str = "unix:///var/run/docker.sock"
    log_level: str = "INFO"

    # Extra (not in the root .env contract, safe defaults): allows a dev to
    # widen CORS without editing code. Comma-separated origins.
    hermes_extra_cors_origins: str = Field(
        default="", description="Comma-separated extra CORS origins for local dev."
    )

    # ------------------------------------------------------------------ #
    # validators
    # ------------------------------------------------------------------ #
    @field_validator("log_level", mode="before")
    @classmethod
    def _upper_log_level(cls, v: Any) -> str:
        s = str(v or "INFO").strip().upper()
        if s not in {"CRITICAL", "ERROR", "WARNING", "INFO", "DEBUG", "NOTSET"}:
            return "INFO"
        return s

    @field_validator("hermes_sandbox_network", mode="before")
    @classmethod
    def _norm_network(cls, v: Any) -> str:
        s = str(v or "none").strip()
        return s or "none"

    @field_validator("freellmapi_base_url", "linkedin_mcp_url", mode="before")
    @classmethod
    def _strip_url(cls, v: Any) -> str:
        return str(v or "").strip().rstrip() or ""

    @field_validator("hermes_sandbox_memory_mb")
    @classmethod
    def _sane_memory(cls, v: int) -> int:
        # Below ~128MB python itself will not start reliably in the sandbox.
        return max(128, int(v))

    @field_validator("hermes_sandbox_cpus")
    @classmethod
    def _sane_cpus(cls, v: float) -> float:
        return max(0.1, float(v))

    @field_validator("hermes_sandbox_timeout_s")
    @classmethod
    def _sane_timeout(cls, v: int) -> int:
        return max(10, int(v))

    # ------------------------------------------------------------------ #
    # derived: models
    # ------------------------------------------------------------------ #
    @property
    def primary_model(self) -> str | None:
        """Configured primary model id, or None to auto-pick from /v1/models."""
        s = (self.hermes_model_primary or "").strip()
        return s or None

    @property
    def fallback_models(self) -> list[str]:
        """Ordered fallback model ids (deduped, primary removed)."""
        out: list[str] = []
        seen = {(self.hermes_model_primary or "").strip()}
        for chunk in (self.hermes_model_fallbacks or "").replace(";", ",").split(","):
            m = chunk.strip()
            if m and m not in seen:
                seen.add(m)
                out.append(m)
        return out

    # ------------------------------------------------------------------ #
    # derived: paths
    # ------------------------------------------------------------------ #
    @property
    def data_dir(self) -> Path:
        """Writable data root.

        In the container this is `/data` (a compose volume). On a Windows or
        macOS dev box `/data` is usually not creatable, so we transparently fall
        back to `<cwd>/data` and log it once. Every other path helper derives
        from this, so the fallback stays consistent across the process.
        """
        return _resolve_data_dir(self.hermes_data_dir)

    @property
    def db_path(self) -> Path:
        return self.data_dir / "hermes.db"

    @property
    def db_url(self) -> str:
        """SYNC sqlite URL (no aiosqlite — see db.py for the rationale)."""
        return "sqlite:///" + self.db_path.as_posix()

    @property
    def workspace_dir(self) -> Path:
        """Root for per-run sandbox workspaces (bind-mounted into sandboxes).

        NOTE for sandbox.py: the path Hermes writes to and the path the Docker
        daemon binds must be the same string when core runs in a container that
        shares /data with the daemon's view. Keep HERMES_SANDBOX_WORKSPACE under
        HERMES_DATA_DIR so the compose volume covers both.
        """
        configured = self.hermes_sandbox_workspace or "/data/workspaces"
        return _remap_under_data_dir(configured, self.hermes_data_dir, self.data_dir)

    @property
    def resumes_dir(self) -> Path:
        """Where rendered .docx/.pdf/.txt resumes are persisted for download."""
        return self.data_dir / "resumes"

    @property
    def uploads_dir(self) -> Path:
        """Where user-uploaded base resumes are persisted."""
        return self.data_dir / "uploads"

    def ensure_dirs(self) -> None:
        """Create every directory Hermes writes to. Safe to call repeatedly."""
        for p in (self.data_dir, self.workspace_dir, self.resumes_dir, self.uploads_dir):
            p.mkdir(parents=True, exist_ok=True)

    # ------------------------------------------------------------------ #
    # derived: docker + sandbox
    # ------------------------------------------------------------------ #
    def effective_docker_host(self) -> str:
        """Docker endpoint to connect to.

        Honours HERMES_DOCKER_HOST, but if we're on Windows and the configured
        value is a unix socket (the container default) we switch to the named
        pipe so `python -m hermes` style local debugging works.
        """
        configured = (self.hermes_docker_host or "").strip()
        if sys.platform.startswith("win") and configured.startswith("unix://"):
            return "npipe:////./pipe/docker_engine"
        return configured or "unix:///var/run/docker.sock"

    def sandbox_limits(self) -> dict[str, Any]:
        """Everything sandbox.py needs to create a hardened container.

        Returned as a plain dict (not kwargs) because some keys are metadata for
        the Sandbox DB row / dashboard display, not docker-py arguments.
        """
        network = (self.hermes_sandbox_network or "none").strip()
        disabled = network.lower() in {"none", "", "off", "disabled"}
        return {
            "image": self.hermes_sandbox_image,
            "memory_mb": self.hermes_sandbox_memory_mb,
            "mem_limit": f"{self.hermes_sandbox_memory_mb}m",
            "cpus": self.hermes_sandbox_cpus,
            "nano_cpus": int(self.hermes_sandbox_cpus * 1_000_000_000),
            "timeout_s": self.hermes_sandbox_timeout_s,
            "network": network,
            "network_disabled": disabled,
            "pids_limit": SANDBOX_PIDS_LIMIT,
            "cap_drop": list(SANDBOX_CAP_DROP),
            "security_opt": list(SANDBOX_SECURITY_OPT),
            "user": SANDBOX_USER,
            "read_only": True,
            "tmpfs": dict(SANDBOX_TMPFS),
            "workspace_root": str(self.workspace_dir),
            "work_mount": SANDBOX_WORK_MOUNT,
            "labels": {
                PROJECT_LABEL: PROJECT_LABEL_VALUE,
                ROLE_LABEL: "sandbox",
            },
        }

    # ------------------------------------------------------------------ #
    # derived: CORS
    # ------------------------------------------------------------------ #
    @property
    def cors_origins(self) -> list[str]:
        """Origins allowed to call the API (the dashboard, mainly).

        In production nginx proxies /api on the same origin, so CORS is a
        convenience for `vite dev` on the host.
        """
        ports = {self.hermes_dashboard_port, 5173, 4173}  # nginx + vite dev/preview
        origins: list[str] = []
        for host in ("localhost", "127.0.0.1"):
            for port in ports:
                origins.append(f"http://{host}:{port}")
        for extra in (self.hermes_extra_cors_origins or "").split(","):
            e = extra.strip()
            if e and e not in origins:
                origins.append(e)
        return origins

    @property
    def linkedin_viewer_url(self) -> str:
        """noVNC URL for the interactive LinkedIn login container."""
        return f"http://localhost:{self.linkedin_viewer_port}/vnc.html"

    # ------------------------------------------------------------------ #
    # loud validation helpers (call from routes / clients, not at import)
    # ------------------------------------------------------------------ #
    def require_llm(self) -> None:
        """Raise ConfigError unless the LLM router is usable."""
        missing: list[str] = []
        if not (self.freellmapi_base_url or "").strip():
            missing.append("FREELLMAPI_BASE_URL")
        if not (self.freellmapi_key or "").strip():
            missing.append("FREELLMAPI_KEY")
        if missing:
            raise ConfigError(
                "LLM router not configured: missing "
                + ", ".join(missing)
                + ". Open the freellmapi dashboard (port "
                + str(self.freellmapi_port)
                + "), mint a client token that starts with 'freellmapi-', then set "
                "FREELLMAPI_KEY in the root .env and restart hermes-core."
            )
        if not self.freellmapi_key.startswith("freellmapi-"):
            log.warning(
                "FREELLMAPI_KEY does not start with 'freellmapi-'; the router may reject it."
            )

    def require_mcp(self) -> None:
        """Raise ConfigError unless the LinkedIn MCP URL is set."""
        if not (self.linkedin_mcp_url or "").strip():
            raise ConfigError(
                "LINKEDIN_MCP_URL is empty. Expected http://linkedin-mcp:8000/mcp "
                "(the streamable-http endpoint of the linkedin-mcp container)."
            )

    def config_report(self) -> dict[str, Any]:
        """Non-secret snapshot for GET /api/health and the Settings page."""
        return {
            "version_data_dir": str(self.data_dir),
            "db_path": str(self.db_path),
            "llm_base_url": self.freellmapi_base_url,
            "llm_key_present": bool((self.freellmapi_key or "").strip()),
            "llm_key_prefix_ok": (self.freellmapi_key or "").startswith("freellmapi-"),
            "model_primary": self.hermes_model_primary or "(auto)",
            "model_fallbacks": self.fallback_models,
            "linkedin_mcp_url": self.linkedin_mcp_url,
            "docker_host": self.effective_docker_host(),
            "log_level": self.log_level,
            "sandbox": {
                k: v
                for k, v in self.sandbox_limits().items()
                if k in {"image", "memory_mb", "cpus", "timeout_s", "network", "pids_limit"}
            },
        }


# --------------------------------------------------------------------------- #
# data-dir resolution (module level so the fallback decision is made once)
# --------------------------------------------------------------------------- #
_DATA_DIR_CACHE: dict[str, Path] = {}


def _resolve_data_dir(configured: str) -> Path:
    """Return a writable Path for `configured`, falling back to ./data."""
    key = configured or "/data"
    cached = _DATA_DIR_CACHE.get(key)
    if cached is not None:
        return cached

    candidate = Path(key).expanduser()
    try:
        candidate.mkdir(parents=True, exist_ok=True)
        probe = candidate / ".hermes-write-test"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink(missing_ok=True)
        resolved = candidate
    except OSError as exc:  # noqa: BLE001 - we want any OS failure to fall back
        resolved = (Path.cwd() / "data").resolve()
        resolved.mkdir(parents=True, exist_ok=True)
        log.warning(
            "HERMES_DATA_DIR=%s is not writable (%s); falling back to %s. "
            "Inside Docker this should never happen — check the /data volume.",
            key,
            exc,
            resolved,
        )
    _DATA_DIR_CACHE[key] = resolved
    return resolved


def _remap_under_data_dir(configured: str, raw_data_dir: str, resolved_data_dir: Path) -> Path:
    """Keep sub-paths (e.g. the workspace root) inside the resolved data dir.

    If HERMES_DATA_DIR fell back from /data to ./data, then
    HERMES_SANDBOX_WORKSPACE=/data/workspaces must follow it, otherwise we'd try
    to write to an unwritable absolute path.
    """
    cfg = Path(configured).expanduser()
    raw = Path(raw_data_dir or "/data")
    if resolved_data_dir == raw:
        return cfg
    try:
        rel = cfg.relative_to(raw)
    except ValueError:
        # Configured outside the data dir; respect it verbatim.
        return cfg
    return resolved_data_dir / rel


def _configure_root_logging(level: str) -> None:
    """Basic stdout logging; uvicorn keeps its own handlers."""
    root = logging.getLogger()
    if not root.handlers:
        handler = logging.StreamHandler(stream=sys.stdout)
        handler.setFormatter(
            logging.Formatter("%(asctime)s %(levelname)-7s %(name)s: %(message)s")
        )
        root.addHandler(handler)
    root.setLevel(getattr(logging, level, logging.INFO))
    logging.getLogger("hermes").setLevel(getattr(logging, level, logging.INFO))
    # httpx logs every request at INFO which drowns the run event stream.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)


# Module-level singleton. Import this, do not construct Settings() elsewhere.
settings = Settings()
_configure_root_logging(settings.log_level)


def get_settings() -> Settings:
    """FastAPI-friendly accessor (kept for dependency-injection ergonomics)."""
    return settings


__all__ = [
    "ConfigError",
    "PROJECT_LABEL",
    "PROJECT_LABEL_VALUE",
    "ROLE_LABEL",
    "SANDBOX_CAP_DROP",
    "SANDBOX_PIDS_LIMIT",
    "SANDBOX_SECURITY_OPT",
    "SANDBOX_TMPFS",
    "SANDBOX_USER",
    "SANDBOX_WORK_MOUNT",
    "Settings",
    "get_settings",
    "settings",
]
