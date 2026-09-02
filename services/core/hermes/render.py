"""Resume document rendering — Markdown in, ATS-safe .docx/.txt/.pdf out.

Nothing is rendered in the hermes-core process. The heavy, untrusted-ish work (parsing
model-generated Markdown, driving python-docx, shelling out to LibreOffice) happens inside
an ephemeral, network-disabled sandbox container: :func:`render_resume` injects
``services/sandbox/ats_docx.py`` into the sandbox workspace via
:meth:`SandboxManager.run_python`'s ``files`` argument, invokes it, then copies the
resulting files out of the per-run workspace into a durable directory under
``$HERMES_DATA_DIR/renders/`` so ``Resume.docx_path`` / ``pdf_path`` / ``txt_path`` stay
valid after workspace cleanup.

ATS-safe formatting rules (single column, no tables/images/headers/footers, standard
fonts, real heading paragraphs, "•" bullets, plain-text contact lines) are implemented and
enforced by ``ats_docx.py`` — see that file's module docstring.
"""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from .sandbox import WORK_DIR, SandboxResult, SandboxUnavailableError, get_sandbox
from .settings import settings

logger = logging.getLogger(__name__)

#: Name the helper script gets inside the sandbox workspace (i.e. ``/work/ats_docx.py``).
SCRIPT_NAME = "ats_docx.py"

#: Where a copy of the script is baked into the *sandbox* image (see
#: services/sandbox/Dockerfile). Used as a fallback when hermes-core cannot find its own
#: copy of the source to inject.
IMAGE_SCRIPT_PATH = f"/opt/hermes/{SCRIPT_NAME}"

#: Sentinel wrapper the in-sandbox driver prints around its JSON result, so the payload
#: survives any incidental stdout noise from LibreOffice.
_JSON_BEGIN = "<<<HERMES_RENDER_JSON>>>"
_JSON_END = "<<<END_HERMES_RENDER_JSON>>>"

#: Result filename written inside the workspace (belt-and-braces fallback path).
_RESULT_JSON = "render_result.json"


class RenderError(RuntimeError):
    """Raised when the resume could not be rendered to a .docx."""


def _script_search_paths() -> List[Path]:
    """Candidate locations of ``ats_docx.py`` on the hermes-core filesystem.

    The first hit is read and injected into the sandbox. ``HERMES_ATS_DOCX_PATH`` wins,
    which makes the layout override-able without a rebuild.
    """
    candidates: List[Path] = []
    override = os.getenv("HERMES_ATS_DOCX_PATH", "").strip()
    if override:
        candidates.append(Path(override))
    here = Path(__file__).resolve()
    candidates.extend(
        [
            here.parent / "sandbox_scripts" / SCRIPT_NAME,  # baked into the core image
            here.parent.parent / "sandbox_scripts" / SCRIPT_NAME,  # services/core/sandbox_scripts
            here.parents[2] / "sandbox" / SCRIPT_NAME,  # repo checkout: services/sandbox
            Path("/opt/hermes") / SCRIPT_NAME,
            Path("/app/sandbox_scripts") / SCRIPT_NAME,
        ]
    )
    return candidates


def load_ats_script() -> Optional[str]:
    """Return the ``ats_docx.py`` source, or None if hermes-core has no copy of it."""
    for candidate in _script_search_paths():
        try:
            if candidate.is_file():
                return candidate.read_text(encoding="utf-8")
        except OSError:  # pragma: no cover - unreadable candidate
            continue
    return None


def safe_basename(basename: str) -> str:
    """Filesystem-safe output stem."""
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "-", str(basename or "")).strip("-.")
    return cleaned or "resume"


def _driver_source(*, md_name: str, basename: str, want_pdf: bool, font: str, font_size: float,
                   pdf_timeout: int, script_present: bool) -> str:
    """Python source for ``/work/main.py``: invokes ats_docx.py and reports its JSON."""
    return f'''"""Hermes sandbox driver: run ats_docx.py and echo its JSON result."""
import json, os, subprocess, sys

WORK = {WORK_DIR!r}
INPUT = os.path.join(WORK, {md_name!r})
RESULT = os.path.join(WORK, {_RESULT_JSON!r})
CANDIDATES = [os.path.join(WORK, {SCRIPT_NAME!r}), {IMAGE_SCRIPT_PATH!r}]
SCRIPT_INJECTED = {script_present!r}

def emit(payload, code):
    blob = json.dumps(payload, ensure_ascii=False)
    sys.stdout.write({_JSON_BEGIN!r} + blob + {_JSON_END!r} + "\\n")
    sys.stdout.flush()
    sys.exit(code)

script = next((path for path in CANDIDATES if os.path.isfile(path)), None)
if script is None:
    emit({{
        "ok": False,
        "error": (
            "ats_docx.py was not found. hermes-core could not locate its own copy to inject "
            "(injected=%s) and the sandbox image has no copy at %s. Set HERMES_ATS_DOCX_PATH in "
            "hermes-core, or COPY services/sandbox/ats_docx.py into the image."
        ) % (SCRIPT_INJECTED, CANDIDATES[-1]),
    }}, 3)

argv = [
    sys.executable, script,
    "--input", INPUT,
    "--outdir", WORK,
    "--basename", {basename!r},
    "--font", {font!r},
    "--font-size", str({font_size!r}),
    "--pdf-timeout", str({pdf_timeout!r}),
    "--json-out", RESULT,
]
if not {want_pdf!r}:
    argv.append("--no-pdf")

proc = subprocess.run(argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
stdout = proc.stdout.decode("utf-8", "replace")
stderr = proc.stderr.decode("utf-8", "replace")
if stderr.strip():
    sys.stderr.write(stderr)

payload = None
if os.path.isfile(RESULT):
    try:
        with open(RESULT, "r", encoding="utf-8") as handle:
            payload = json.load(handle)
    except Exception as exc:
        sys.stderr.write("could not read result json: %s\\n" % exc)
if payload is None:
    # Fall back to the last JSON object ats_docx.py printed on stdout.
    for line in reversed([l for l in stdout.splitlines() if l.strip().startswith("{{")]):
        try:
            payload = json.loads(line)
            break
        except Exception:
            continue
if payload is None:
    payload = {{
        "ok": False,
        "error": "ats_docx.py produced no parseable result (exit %s)" % proc.returncode,
        "stdout_tail": stdout[-2000:],
        "stderr_tail": stderr[-2000:],
    }}
payload.setdefault("exit_code", proc.returncode)
emit(payload, proc.returncode)
'''


def _extract_payload(result: SandboxResult) -> Dict[str, Any]:
    """Pull the driver's JSON out of stdout, falling back to the workspace result file."""
    stdout = result.stdout or ""
    start = stdout.find(_JSON_BEGIN)
    end = stdout.find(_JSON_END, start + 1) if start >= 0 else -1
    if start >= 0 and end > start:
        blob = stdout[start + len(_JSON_BEGIN) : end]
        try:
            return json.loads(blob)
        except json.JSONDecodeError as exc:  # pragma: no cover - sentinel corruption
            logger.warning("sentinel JSON was unparseable: %s", exc)

    # Fallback: the driver also writes render_result.json into the workspace.
    for artifact in result.artifacts:
        if Path(artifact).name == _RESULT_JSON:
            try:
                return json.loads(Path(artifact).read_text(encoding="utf-8"))
            except Exception as exc:  # pragma: no cover
                logger.warning("could not read %s: %s", artifact, exc)
    return {}


def _durable_dir(basename: str) -> Path:
    """Create (and return) a stable output directory under ``$HERMES_DATA_DIR/renders``."""
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    target = Path(settings.hermes_data_dir) / "renders" / f"{basename}-{stamp}"
    target.mkdir(parents=True, exist_ok=True)
    return target


async def render_resume(
    markdown: str,
    basename: str,
    run_id: Optional[str] = None,
    *,
    want_pdf: bool = True,
    font: str = "Calibri",
    font_size: float = 11.0,
    timeout: Optional[int] = None,
) -> Dict[str, Any]:
    """Render resume Markdown into ATS-safe documents inside the sandbox.

    Args:
        markdown: The resume body produced by :class:`ResumeArchitect`.
        basename: Output file stem (sanitised; e.g. ``"jane-doe-v3"``).
        run_id: Run to stream sandbox progress events to.
        want_pdf: Attempt the LibreOffice PDF conversion (skipped gracefully if absent).
        font: Body font; ``ats_docx.py`` clamps this to its ATS-safe list.
        font_size: Body size in points; clamped to 10-12pt.
        timeout: Sandbox wall-clock limit; defaults to ``HERMES_SANDBOX_TIMEOUT_S``.

    Returns:
        ``{"docx": path, "txt": path, "pdf": path|None, ...}`` — absolute paths on the
        hermes-core filesystem (under ``$HERMES_DATA_DIR/renders/``), plus ``warnings``,
        ``stats``, ``pdf_error``, ``markdown_path`` and ``sandbox`` diagnostics.

    Raises:
        ValueError: ``markdown`` is empty.
        SandboxUnavailableError: Docker is unreachable or the sandbox image is missing.
        RenderError: The sandbox ran but no .docx was produced.
    """
    if not markdown or not markdown.strip():
        raise ValueError("render_resume() requires non-empty resume markdown")

    stem = safe_basename(basename)
    md_name = f"{stem}.md"
    script_source = load_ats_script()
    if script_source is None:
        # This is the NORMAL path for the shipped stack, not a fault: the core
        # image's build context is services/core, so it cannot copy in
        # services/sandbox/ats_docx.py, and the sandbox image bakes the script
        # at IMAGE_SCRIPT_PATH instead. Keeping the script in exactly one place
        # also stops the two copies from drifting. Overriding it from the core
        # filesystem stays supported for local iteration on the renderer, which
        # is why the lookup runs at all.
        logger.debug(
            "using the ats_docx.py baked into the sandbox image at %s (no override found on the "
            "hermes-core filesystem; searched: %s)",
            IMAGE_SCRIPT_PATH,
            ", ".join(str(p) for p in _script_search_paths()),
        )

    files: Dict[str, str] = {md_name: markdown}
    if script_source is not None:
        files[SCRIPT_NAME] = script_source

    sandbox = get_sandbox()
    effective_timeout = int(timeout if timeout is not None else settings.hermes_sandbox_timeout_s)
    driver = _driver_source(
        md_name=md_name,
        basename=stem,
        want_pdf=bool(want_pdf),
        font=font,
        font_size=float(font_size),
        # Leave the sandbox a little headroom so we get the JSON back instead of a hard kill.
        pdf_timeout=max(30, effective_timeout - 30),
        script_present=script_source is not None,
    )

    try:
        result = await sandbox.run_python(
            driver,
            files=files,
            timeout=effective_timeout,
            run_id=run_id,
            network="none",  # rendering never needs the network
        )
    except SandboxUnavailableError:
        raise  # already carries actionable remediation text

    payload = _extract_payload(result)

    if not payload.get("ok"):
        detail = payload.get("error") or "no error detail returned"
        raise RenderError(
            f"Resume rendering failed (sandbox exit {result.exit_code}"
            f"{', timed out' if result.timed_out else ''}): {detail}\n"
            f"stderr tail: {(result.stderr or '')[-1500:]}"
        )

    # The sandbox reports /work-relative paths; map them onto the per-run workspace, then
    # copy into a durable directory so the Resume row keeps working after cleanup.
    workspace = Path(result.workspace) if result.workspace else None
    out_dir = _durable_dir(stem)

    def _materialise(sandbox_path: Optional[str]) -> Optional[str]:
        if not sandbox_path:
            return None
        name = Path(str(sandbox_path).replace("\\", "/")).name
        source = None
        if workspace is not None and (workspace / name).is_file():
            source = workspace / name
        else:
            for artifact in result.artifacts:
                if Path(artifact).name == name:
                    source = Path(artifact)
                    break
        if source is None:
            logger.warning("sandbox reported %s but no such file was found in the workspace", sandbox_path)
            return None
        destination = out_dir / name
        try:
            shutil.copy2(source, destination)
            return str(destination)
        except OSError as exc:  # pragma: no cover - disk full / permissions
            logger.warning("could not copy %s to %s: %s; returning workspace path", source, destination, exc)
            return str(source)

    docx_path = _materialise(payload.get("docx"))
    txt_path = _materialise(payload.get("txt"))
    pdf_path = _materialise(payload.get("pdf"))

    if not docx_path:
        raise RenderError(
            "Resume rendering reported success but produced no .docx file. "
            f"Sandbox workspace: {result.workspace}; artifacts: {result.artifacts}"
        )

    # Keep the source markdown next to the rendered files (handy for ?fmt=md downloads).
    markdown_path = out_dir / f"{stem}.md"
    try:
        markdown_path.write_text(markdown, encoding="utf-8")
    except OSError as exc:  # pragma: no cover
        logger.warning("could not persist markdown copy: %s", exc)

    warnings = list(payload.get("warnings") or [])
    pdf_error = payload.get("pdf_error")
    if pdf_path is None and want_pdf and not pdf_error:
        pdf_error = "PDF was not produced (no reason reported by the renderer)."

    return {
        "docx": docx_path,
        "txt": txt_path,
        "pdf": pdf_path,
        "pdf_error": pdf_error,
        "markdown_path": str(markdown_path) if markdown_path.exists() else None,
        "output_dir": str(out_dir),
        "warnings": warnings,
        "stats": payload.get("stats") or {},
        "sandbox": {
            "container_id": result.container_id,
            "exit_code": result.exit_code,
            "timed_out": result.timed_out,
            "duration_s": round(result.duration_s, 3),
            "workspace": result.workspace,
        },
    }


__all__ = ["render_resume", "RenderError", "load_ats_script", "safe_basename", "SCRIPT_NAME", "IMAGE_SCRIPT_PATH"]
