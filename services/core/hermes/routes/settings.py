"""``GET /api/settings`` / ``PUT /api/settings`` — the dashboard-editable rows.

Three rules this module enforces, because they are the ones that bite:

1. **Secrets never leave the process.** ``FREELLMAPI_KEY`` and
   ``ENCRYPTION_KEY`` live in the root ``.env``, never in the ``setting`` table.
   The read side reports the key as ``freellmapi-***`` (present / absent, prefix
   valid) and the write side refuses those keys outright with a message telling
   the operator where the value actually belongs.
2. **Only known keys are writable.** The editable set is
   ``hermes.db.default_settings()`` plus whatever rows already exist. A typo
   would otherwise create a dead row the dashboard shows forever.
3. **Values are validated, and a model change actually takes effect.**
   ``LLMRouter`` is constructed from the environment-backed ``settings``
   singleton, so writing ``model_primary`` to the DB alone would change nothing.
   Saving those two keys therefore also updates the live ``settings`` object and
   drops the cached router, so the next LLM call uses the new chain.

The response deliberately carries more than the raw rows: ``env`` is a
non-secret configuration snapshot and ``missing`` / ``issues`` say which required
configuration is absent, so the Settings page can render a real diagnosis
instead of an empty form.
"""

from __future__ import annotations

import logging
from typing import Any, Callable, Mapping

from fastapi import APIRouter, Body, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from hermes.db import default_settings, get_db
from hermes.models import Setting
from hermes.routes._common import (
    as_bool,
    csv_list,
    iso,
    set_setting,
    validate_setting_key,
)
from hermes.settings import ConfigError, settings

log = logging.getLogger("hermes.api.settings")

router = APIRouter(tags=["settings"])

#: Never readable, never writable through the API.
SECRET_KEYS: frozenset[str] = frozenset(
    {
        "freellmapi_key",
        "encryption_key",
        "freellmapi_api_key",
        "openai_api_key",
        "api_key",
        "linkedin_password",
        "linkedin_email",
    }
)

#: Longest value a single Setting row may hold (keeps a paste accident out of the DB).
MAX_VALUE_CHARS = 4000

#: Writing either of these has to reach the live LLM router, not just the table.
LLM_KEYS: frozenset[str] = frozenset({"model_primary", "model_fallbacks"})


# --------------------------------------------------------------------------- #
# value validation
# --------------------------------------------------------------------------- #


def _int_in(low: int, high: int) -> Callable[[str, str], str]:
    def _check(key: str, value: str) -> str:
        try:
            number = int(str(value).strip())
        except (TypeError, ValueError):
            raise HTTPException(
                status_code=422,
                detail=f"{key} must be a whole number between {low} and {high} (got {value!r}).",
            ) from None
        if not low <= number <= high:
            raise HTTPException(
                status_code=422,
                detail=f"{key} must be between {low} and {high} (got {number}).",
            )
        return str(number)

    return _check


def _float_in(low: float, high: float) -> Callable[[str, str], str]:
    def _check(key: str, value: str) -> str:
        try:
            number = float(str(value).strip())
        except (TypeError, ValueError):
            raise HTTPException(
                status_code=422,
                detail=f"{key} must be a number between {low} and {high} (got {value!r}).",
            ) from None
        if not low <= number <= high:
            raise HTTPException(
                status_code=422,
                detail=f"{key} must be between {low} and {high} (got {number}).",
            )
        return f"{number:g}"

    return _check


def _boolean(key: str, value: str) -> str:
    text = str(value).strip().lower()
    if text in ("", "1", "0", "true", "false", "yes", "no", "y", "n", "on", "off"):
        return "true" if as_bool(text, False) else "false"
    raise HTTPException(
        status_code=422,
        detail=f"{key} must be a boolean (true/false, got {value!r}).",
    )


def _model_id(key: str, value: str) -> str:
    """A model id, or blank to let the router auto-pick from ``GET /v1/models``."""
    text = str(value).strip()
    if len(text) > 200:
        raise HTTPException(status_code=422, detail=f"{key} is too long to be a model id.")
    if any(char in text for char in (" ", ",", "\n", "\t")):
        raise HTTPException(
            status_code=422,
            detail=(
                f"{key} takes exactly one model id (no spaces or commas). "
                "Use model_fallbacks for the comma-separated failover chain."
            ),
        )
    return text


def _model_chain(key: str, value: str) -> str:
    """Comma-separated failover chain, deduped and whitespace-normalised."""
    chain = csv_list(value)
    for item in chain:
        _model_id(key, item)
    return ",".join(chain)


def _free_text(key: str, value: str) -> str:
    text = str(value if value is not None else "").strip()
    if len(text) > MAX_VALUE_CHARS:
        raise HTTPException(
            status_code=422,
            detail=f"{key} is longer than {MAX_VALUE_CHARS} characters.",
        )
    return text


#: key -> validator. Keys without an entry fall back to :func:`_free_text`.
VALIDATORS: dict[str, Callable[[str, str], str]] = {
    "model_primary": _model_id,
    "model_fallbacks": _model_chain,
    "job_search_easy_apply": _boolean,
    "job_search_max_pages": _int_in(1, 10),
    "job_min_score": _int_in(0, 100),
    "resume_target_pages": _int_in(1, 3),
    "llm_temperature": _float_in(0.0, 2.0),
}


# --------------------------------------------------------------------------- #
# read helpers
# --------------------------------------------------------------------------- #


def _mask_secret(value: str | None) -> str:
    """Render a secret as a shape hint, never as a value."""
    text = str(value or "").strip()
    if not text:
        return ""
    if text.startswith("freellmapi-"):
        return "freellmapi-***"
    return "***"


def _rows(db: Session) -> list[Setting]:
    return list(db.execute(select(Setting).order_by(Setting.key)).scalars().all())


def _editable_keys(db: Session) -> set[str]:
    """Defaults seeded by ``init_db()`` plus any row already in the table."""
    keys = set(default_settings().keys())
    keys.update(row.key for row in _rows(db))
    return keys - SECRET_KEYS


def _env_snapshot() -> dict[str, Any]:
    """Non-secret configuration facts for the Settings page."""
    report = dict(settings.config_report())
    report.update(
        {
            "freellmapi_key": _mask_secret(settings.freellmapi_key),
            "freellmapi_base_url": settings.freellmapi_base_url,
            "linkedin_mcp_url": settings.linkedin_mcp_url,
            "linkedin_viewer_url": settings.linkedin_viewer_url,
            "linkedin_viewer_port": settings.linkedin_viewer_port,
            "hermes_api_port": settings.hermes_api_port,
            "hermes_dashboard_port": settings.hermes_dashboard_port,
            "freellmapi_port": settings.freellmapi_port,
            "cors_origins": settings.cors_origins,
            # Stated here so the UI never has to guess: there is no apply tool.
            "auto_apply_supported": False,
        }
    )
    return report


def _config_problems() -> tuple[list[str], list[str]]:
    """``(missing env var names, human-readable issues)``."""
    missing: list[str] = []
    issues: list[str] = []

    try:
        settings.require_llm()
    except ConfigError as exc:
        issues.append(str(exc))
        if not (settings.freellmapi_base_url or "").strip():
            missing.append("FREELLMAPI_BASE_URL")
        if not (settings.freellmapi_key or "").strip():
            missing.append("FREELLMAPI_KEY")

    if (settings.freellmapi_key or "").strip() and not settings.freellmapi_key.startswith(
        "freellmapi-"
    ):
        issues.append(
            "FREELLMAPI_KEY does not start with 'freellmapi-'. That is the shape the "
            "router mints; the current value will probably be rejected with a 401."
        )

    try:
        settings.require_mcp()
    except ConfigError as exc:
        missing.append("LINKEDIN_MCP_URL")
        issues.append(str(exc))

    return missing, issues


def _payload(db: Session, applied: list[str] | None = None) -> dict[str, Any]:
    """The GET/PUT response body (identical shape for both verbs)."""
    rows = [row for row in _rows(db) if row.key not in SECRET_KEYS]
    missing, issues = _config_problems()
    return {
        "items": [
            {"key": row.key, "value": row.value or "", "updated_at": iso(row.updated_at)}
            for row in rows
        ],
        "values": {row.key: (row.value or "") for row in rows},
        "env": _env_snapshot(),
        "sandbox_limits": settings.sandbox_limits(),
        "editable_keys": sorted(_editable_keys(db)),
        "missing": missing,
        "issues": issues,
        "config_ok": not missing,
        "applied": applied or [],
    }


# --------------------------------------------------------------------------- #
# write helpers
# --------------------------------------------------------------------------- #


def _unwrap_body(payload: Any) -> Mapping[str, Any]:
    """Accept ``{...}``, ``{"values": {...}}`` and ``{"settings": {...}}``.

    The dashboard's ``api.putSettings()`` sends a flat map; ``SettingsUpdate``
    in schemas.py wraps it under ``values``. Both are legal here.
    """
    if not isinstance(payload, Mapping):
        raise HTTPException(
            status_code=422,
            detail=(
                "PUT /api/settings expects a JSON object of {key: value} pairs "
                f"(got {type(payload).__name__})."
            ),
        )
    for wrapper in ("values", "settings"):
        inner = payload.get(wrapper)
        if isinstance(inner, Mapping):
            return inner
    return payload


def _coerce_value(key: str, raw: Any) -> str:
    if isinstance(raw, bool):
        raw = "true" if raw else "false"
    elif isinstance(raw, (int, float)):
        raw = str(raw)
    elif raw is None:
        raw = ""
    elif not isinstance(raw, str):
        raise HTTPException(
            status_code=422,
            detail=(
                f"Setting {key!r} must be a string, number or boolean "
                f"(got {type(raw).__name__}). Settings are stored as text."
            ),
        )
    validator = VALIDATORS.get(key, _free_text)
    return validator(key, raw)


async def _apply_llm_change(values: dict[str, str]) -> list[str]:
    """Make a saved model choice take effect without a container restart.

    ``LLMRouter`` reads ``settings.hermes_model_primary`` /
    ``hermes_model_fallbacks`` (env-backed) at construction time, so the DB row
    alone would be inert. Mirror the new value onto the live settings object and
    drop the cached router.
    """
    notes: list[str] = []
    if "model_primary" in values:
        settings.hermes_model_primary = values["model_primary"]
        notes.append(f"primary model -> {values['model_primary'] or '(auto-pick)'}")
    if "model_fallbacks" in values:
        settings.hermes_model_fallbacks = values["model_fallbacks"]
        notes.append(f"fallback chain -> {values['model_fallbacks'] or '(none)'}")
    if not notes:
        return []

    try:
        from hermes.llm import reset_llm

        await reset_llm()
        notes.append("LLM router reloaded")
    except Exception as exc:  # noqa: BLE001 - saving must not fail on this
        log.warning("could not reload the LLM router after a settings change: %s", exc)
        notes.append(
            f"LLM router could NOT be reloaded ({type(exc).__name__}: {exc}); "
            "restart hermes-core to pick up the new model."
        )
    return notes


# --------------------------------------------------------------------------- #
# routes
# --------------------------------------------------------------------------- #


@router.get("/settings", summary="Editable settings + non-secret config snapshot")
def get_settings_route(db: Session = Depends(get_db)) -> dict[str, Any]:
    """Every editable ``Setting`` row, plus what configuration is still missing."""
    return _payload(db)


@router.put("/settings", summary="Upsert editable settings")
async def put_settings_route(
    payload: Any = Body(default=None),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    """Upsert the given keys.

    Body may be a flat ``{key: value}`` map (what the dashboard sends), or the
    same map wrapped under ``values`` / ``settings``. Unknown keys are rejected
    rather than silently created; secrets are rejected with a pointer to ``.env``.
    """
    body = _unwrap_body(payload if payload is not None else {})
    if not body:
        allowed = ", ".join(sorted(_editable_keys(db)))
        raise HTTPException(
            status_code=422,
            detail=(
                "PUT /api/settings received an empty body. Send a JSON object of "
                f"{{key: value}} pairs. Editable keys: {allowed}."
            ),
        )

    allowed_keys = _editable_keys(db)
    updates: dict[str, str] = {}
    for raw_key, raw_value in body.items():
        key = validate_setting_key(str(raw_key)).strip()
        if key.lower() in SECRET_KEYS:
            raise HTTPException(
                status_code=400,
                detail=(
                    f"{key} is a secret and is never stored in the database. Set it in "
                    "the root .env file (FREELLMAPI_KEY / ENCRYPTION_KEY) and restart "
                    "hermes-core."
                ),
            )
        if key not in allowed_keys:
            raise HTTPException(
                status_code=422,
                detail=(
                    f"Unknown setting {key!r}. Editable keys: "
                    f"{', '.join(sorted(allowed_keys))}."
                ),
            )
        updates[key] = _coerce_value(key, raw_value)

    for key, value in updates.items():
        set_setting(db, key, value)
    db.commit()
    log.info("settings updated: %s", ", ".join(sorted(updates)))

    applied: list[str] = [f"saved {len(updates)} setting(s)"]
    if LLM_KEYS & set(updates):
        applied.extend(await _apply_llm_change(updates))

    return _payload(db, applied=applied)
