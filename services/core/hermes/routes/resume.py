"""``/api/resume*`` + ``/api/resumes*`` — upload, generate, list, download, score.

Four responsibilities:

* **Upload** (``POST /api/resume/upload``) — the only *synchronous* piece of
  work in this module. The uploaded file is persisted under
  ``settings.uploads_dir``, its text is extracted (``.pdf`` via pypdf, ``.docx``
  via docx2txt, ``.txt``/``.md`` read directly) and stored in **two** places:

  1. the ``uploaded_resume_text`` Setting row — which is exactly what
     ``pipeline._step_build_resume`` reads as "base resume" source material, and
  2. a ``Resume`` row, so the file shows up on the Resumes page and can be
     downloaded straight back.

  Anything else (``.doc``, ``.rtf``, ``.pages``, images…) is rejected with
  ``415`` and the conversion step to take.

* **Generate / score** — fire-and-forget runs; the dashboard follows
  ``GET /api/runs/{id}/events``.

* **List / read** — newest first, markdown omitted from list rows.

* **Download** — a real ``FileResponse`` per format. ``?fmt=md`` is always
  available (the markdown lives in the DB and is materialised on demand);
  ``?fmt=pdf`` legitimately 404s when LibreOffice was unavailable inside the
  sandbox at render time, and the error says so.
"""

from __future__ import annotations

import logging
import os
import re
from pathlib import Path
from typing import Any, Optional

from fastapi import APIRouter, Body, Depends, File, Form, HTTPException, Query, UploadFile
from fastapi.responses import FileResponse
from sqlalchemy import func, select
from sqlalchemy.orm import Session
from starlette.concurrency import run_in_threadpool

from hermes.db import get_db
from hermes.models import Job, Profile, Resume
from hermes.routes._common import (
    available_formats,
    coerce_pk,
    resume_dict,
    run_dict,
    safe_basename,
    set_setting,
    utcnow,
)
from hermes.runner import UnknownRunKind, start_run
from hermes.schemas import ResumeGenerateRequest, ResumeScoreRequest
from hermes.settings import settings

log = logging.getLogger("hermes.api.resume")

router = APIRouter(tags=["resume"])

#: Hard cap on an uploaded resume. A real resume is < 2 MB; anything larger is
#: a mistake (or an attempt to fill the data volume).
MAX_UPLOAD_BYTES = 15 * 1024 * 1024

#: Below this, extraction effectively failed (scanned PDF, empty document).
MIN_TEXT_CHARS = 40

#: extension -> ("pdf" | "docx" | "text"), i.e. how to get text out of it.
_EXTRACTORS: dict[str, str] = {
    ".pdf": "pdf",
    ".docx": "docx",
    ".txt": "text",
    ".md": "text",
    ".markdown": "text",
}

#: Formats ``/resumes/{id}/download`` can serve, with their media types.
_MEDIA_TYPES: dict[str, str] = {
    "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "pdf": "application/pdf",
    "txt": "text/plain; charset=utf-8",
    "md": "text/markdown; charset=utf-8",
}

#: Files we know how to reject with a *useful* message.
_CONVERT_HINTS: dict[str, str] = {
    ".doc": "Open it in Word/LibreOffice and 'Save As' .docx or .pdf.",
    ".rtf": "Open it in Word/LibreOffice and 'Save As' .docx or .pdf.",
    ".odt": "Open it in LibreOffice and 'Save As' .docx or .pdf.",
    ".pages": "Export it from Pages as .pdf or .docx.",
    ".html": "Print it to PDF, or paste the text into a .md file.",
    ".htm": "Print it to PDF, or paste the text into a .md file.",
    ".jpg": "Hermes cannot OCR images. Export a text-based .pdf or .docx.",
    ".jpeg": "Hermes cannot OCR images. Export a text-based .pdf or .docx.",
    ".png": "Hermes cannot OCR images. Export a text-based .pdf or .docx.",
}

_BLANK_RUN_RE = re.compile(r"\n{3,}")
_TRAILING_WS_RE = re.compile(r"[ \t]+(?=\n)")


# --------------------------------------------------------------------------- #
# local helpers (self-contained per route module, matching routes/health.py)
# --------------------------------------------------------------------------- #


def _run_response(run: Any) -> dict[str, Any]:
    """``run_dict`` plus the ``*_json`` aliases the dashboard client reads."""
    payload = run_dict(run)
    payload["params_json"] = payload.get("params")
    payload["result_json"] = payload.get("result")
    return payload


def _start(db: Session, kind: str, params: dict[str, Any]) -> dict[str, Any]:
    """Schedule a run, translating runner failures into honest HTTP errors."""
    try:
        run = start_run(db, kind, params)
    except UnknownRunKind as exc:  # pragma: no cover - guarded by RUN_KINDS
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(
            status_code=422, detail=f"Cannot start the {kind} run: {exc}"
        ) from exc
    except RuntimeError as exc:
        raise HTTPException(
            status_code=500, detail=f"Cannot start the {kind} run: {exc}"
        ) from exc
    return _run_response(run)


def _resume_response(resume: Resume, *, full: bool) -> dict[str, Any]:
    """``resume_dict`` plus the flat/aliased keys the dashboard client reads.

    ``src/lib/types.ts::Resume`` expects the raw column names
    (``ats_breakdown_json``, ``docx_path``…) while ``_common.resume_dict``
    nests paths under ``paths`` and parses the breakdown into
    ``ats_breakdown``. Both spellings are emitted; the breakdown is small
    (six subscores + short string lists) so it is cheap even in list rows.
    """
    payload = resume_dict(resume, full=full)
    payload["available_formats"] = list(payload.get("formats") or [])
    payload["docx_path"] = getattr(resume, "docx_path", None)
    payload["pdf_path"] = getattr(resume, "pdf_path", None)
    payload["txt_path"] = getattr(resume, "txt_path", None)
    breakdown = payload.get("ats_breakdown")
    if breakdown is None:
        breakdown = dict(getattr(resume, "ats_breakdown", {}) or {})
        payload["ats_breakdown"] = breakdown
    payload["ats_breakdown_json"] = breakdown
    if not full:
        # Enough to render a card without a detail fetch, without shipping the
        # whole document in a list response.
        markdown = getattr(resume, "markdown", "") or ""
        payload["markdown_chars"] = len(markdown)
        payload["markdown_preview"] = markdown[:400]
    return payload


def _get_resume_or_404(db: Session, resume_id: str) -> Resume:
    resume = db.get(Resume, coerce_pk(Resume, resume_id))
    if resume is None:
        raise HTTPException(
            status_code=404,
            detail=(
                f"Resume {resume_id!r} not found. GET /api/resumes lists the "
                "resumes that exist."
            ),
        )
    return resume


def _next_version(db: Session, profile_id: Optional[str]) -> int:
    """Next ``Resume.version`` for a profile (``profile_id=None`` is a group)."""
    current = db.execute(
        select(func.max(Resume.version)).where(Resume.profile_id == profile_id)
    ).scalar()
    return int(current or 0) + 1


def _latest_profile_id(db: Session) -> Optional[str]:
    return (
        db.execute(
            select(Profile.id)
            .order_by(Profile.fetched_at.desc(), Profile.id.desc())
            .limit(1)
        )
        .scalars()
        .first()
    )


def _clean_text(raw: str) -> str:
    """Normalise extracted text: LF endings, no trailing spaces, no huge gaps."""
    text = (raw or "").replace("\r\n", "\n").replace("\r", "\n")
    text = _TRAILING_WS_RE.sub("", text)
    text = _BLANK_RUN_RE.sub("\n\n", text)
    return text.strip()


# --------------------------------------------------------------------------- #
# text extraction (blocking; always called through run_in_threadpool)
# --------------------------------------------------------------------------- #


def _extract_pdf(path: Path, filename: str) -> str:
    try:
        from pypdf import PdfReader  # noqa: PLC0415 - optional-at-runtime import
    except Exception as exc:  # pragma: no cover - dependency is pinned
        raise HTTPException(
            status_code=500,
            detail=(
                "PDF text extraction needs the 'pypdf' package, which is not "
                f"importable in hermes-core ({type(exc).__name__}: {exc}). It is "
                "pinned in services/core/requirements.txt — rebuild the image "
                "(`make build`)."
            ),
        ) from exc

    try:
        reader = PdfReader(str(path))
        if getattr(reader, "is_encrypted", False):
            # Many resumes are "protected" with an empty owner password.
            try:
                reader.decrypt("")
            except Exception:  # pragma: no cover - genuinely locked file
                raise HTTPException(
                    status_code=422,
                    detail=(
                        f"{filename} is password-protected. Remove the password "
                        "(or export an unprotected copy) and upload it again."
                    ),
                ) from None
        pages = [(page.extract_text() or "") for page in reader.pages]
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(
            status_code=422,
            detail=(
                f"Could not read {filename} as a PDF ({type(exc).__name__}: {exc}). "
                "Re-export it from your editor, or upload the .docx instead."
            ),
        ) from exc
    return _clean_text("\n\n".join(pages))


def _extract_docx(path: Path, filename: str) -> str:
    try:
        import docx2txt  # noqa: PLC0415 - optional-at-runtime import
    except Exception as exc:  # pragma: no cover - dependency is pinned
        raise HTTPException(
            status_code=500,
            detail=(
                "DOCX text extraction needs the 'docx2txt' package, which is not "
                f"importable in hermes-core ({type(exc).__name__}: {exc}). It is "
                "pinned in services/core/requirements.txt — rebuild the image "
                "(`make build`)."
            ),
        ) from exc

    try:
        return _clean_text(docx2txt.process(str(path)) or "")
    except Exception as exc:
        raise HTTPException(
            status_code=422,
            detail=(
                f"Could not read {filename} as a .docx ({type(exc).__name__}: {exc}). "
                "If it is really an old .doc, re-save it as .docx first."
            ),
        ) from exc


def _extract_plain(data: bytes, filename: str) -> str:
    for encoding in ("utf-8", "utf-8-sig", "cp1252", "latin-1"):
        try:
            return _clean_text(data.decode(encoding))
        except UnicodeDecodeError:
            continue
    log.warning("undecodable text upload %s; falling back to replacement chars", filename)
    return _clean_text(data.decode("utf-8", errors="replace"))


# --------------------------------------------------------------------------- #
# POST /resume/upload
# --------------------------------------------------------------------------- #


@router.post("/resume/upload", summary="Upload a base resume and extract its text")
async def upload_resume(
    file: UploadFile = File(..., description="A .pdf, .docx, .txt or .md resume."),
    label: Optional[str] = Form(
        default=None, description="Optional label for the stored Resume row."
    ),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    """Store an uploaded resume and its extracted text.

    The text becomes the ``uploaded_resume_text`` setting, which every later
    ``resume_build`` run uses as source material (so the generated resume keeps
    your real wording instead of inventing it from the LinkedIn profile alone).

    Returns the stored path, character count, a preview and the ``resume_id``
    of the Resume row created for it.
    """
    original = (file.filename or "").strip() or "resume"
    ext = os.path.splitext(original)[1].lower()
    kind = _EXTRACTORS.get(ext)
    if kind is None:
        hint = _CONVERT_HINTS.get(ext)
        raise HTTPException(
            status_code=415,
            detail=(
                f"Unsupported resume format {ext or '(no extension)'!r} for "
                f"{original!r}. Hermes reads .pdf, .docx, .txt and .md."
                + (f" {hint}" if hint else "")
            ),
        )

    data = await file.read(MAX_UPLOAD_BYTES + 1)
    await file.close()
    if not data:
        raise HTTPException(
            status_code=422, detail=f"{original!r} is empty (0 bytes)."
        )
    if len(data) > MAX_UPLOAD_BYTES:
        raise HTTPException(
            status_code=413,
            detail=(
                f"{original!r} is larger than the "
                f"{MAX_UPLOAD_BYTES // (1024 * 1024)} MB upload limit. A resume "
                "should be well under 2 MB — export it without embedded images."
            ),
        )

    # ---- persist the original ------------------------------------------- #
    try:
        settings.ensure_dirs()
        uploads = settings.uploads_dir
        stem = safe_basename(os.path.splitext(original)[0], "resume")
        stamp = utcnow().strftime("%Y%m%dT%H%M%SZ")
        stored = uploads / f"{stamp}_{stem}{ext}"
        stored.write_bytes(data)
    except OSError as exc:
        raise HTTPException(
            status_code=500,
            detail=(
                f"Could not write the upload to {settings.uploads_dir} "
                f"({type(exc).__name__}: {exc}). Check that the hermes-data volume "
                "is mounted and writable."
            ),
        ) from exc

    # ---- extract text ---------------------------------------------------- #
    if kind == "pdf":
        text = await run_in_threadpool(_extract_pdf, stored, original)
    elif kind == "docx":
        text = await run_in_threadpool(_extract_docx, stored, original)
    else:
        text = await run_in_threadpool(_extract_plain, data, original)

    if len(text) < MIN_TEXT_CHARS:
        raise HTTPException(
            status_code=422,
            detail=(
                f"Only {len(text)} characters of text could be extracted from "
                f"{original!r} (the file is kept at {stored}). This usually means a "
                "scanned/image-only PDF — Hermes does not OCR. Export a text-based "
                "PDF, upload the .docx, or paste the content into a .md file."
            ),
        )

    # ---- extracted-text sidecar (makes ?fmt=txt work for uploads) -------- #
    txt_path: Optional[str] = str(stored) if kind == "text" else None
    if txt_path is None:
        sidecar = stored.with_suffix(".extracted.txt")
        try:
            sidecar.write_text(text, encoding="utf-8", newline="\n")
            txt_path = str(sidecar)
        except OSError as exc:  # pragma: no cover - non-fatal
            log.warning("could not write extracted-text sidecar %s: %s", sidecar, exc)

    # ---- persist: settings + Resume row ---------------------------------- #
    profile_id = _latest_profile_id(db)
    version = _next_version(db, profile_id)
    resume = Resume(
        profile_id=profile_id,
        version=version,
        label=(label or "").strip() or f"Uploaded — {original}",
        markdown=text,
        docx_path=str(stored) if ext == ".docx" else None,
        pdf_path=str(stored) if ext == ".pdf" else None,
        txt_path=txt_path,
        created_at=utcnow(),
    )
    db.add(resume)

    set_setting(db, "uploaded_resume_text", text)
    set_setting(db, "uploaded_resume_name", original)
    set_setting(db, "uploaded_resume_path", str(stored))
    set_setting(db, "uploaded_resume_chars", len(text))
    set_setting(db, "uploaded_resume_at", utcnow().isoformat())
    db.commit()
    db.refresh(resume)

    log.info(
        "stored uploaded resume %s (%d chars) as resume %s v%d",
        stored,
        len(text),
        resume.id,
        version,
    )
    return {
        "filename": original,
        "format": ext.lstrip("."),
        "chars": len(text),
        "stored_path": str(stored),
        "preview": text[:1200],
        "profile_id": profile_id,
        "resume_id": resume.id,
        "resume": _resume_response(resume, full=False),
        "detail": (
            "Stored as base resume source material. POST /api/resume/generate now "
            "rewrites it into an ATS-safe resume; POST /api/jobs/{id}/tailor targets "
            "a specific posting."
        ),
    }


# --------------------------------------------------------------------------- #
# POST /resume/generate
# --------------------------------------------------------------------------- #


@router.post("/resume/generate", summary="Generate an ATS-optimised resume")
async def generate_resume(
    payload: Optional[ResumeGenerateRequest] = Body(default=None),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    """Kick off a ``resume_build`` run (architect → render → ATS score).

    With no body it uses the latest imported profile and produces a general
    "base" resume. Pass ``target_job_id`` to aim it at one posting (the same
    thing ``POST /api/jobs/{id}/tailor`` does, plus the job linkage).
    """
    body = payload or ResumeGenerateRequest()
    params: dict[str, Any] = {}

    if body.profile_id:
        if db.get(Profile, coerce_pk(Profile, body.profile_id)) is None:
            raise HTTPException(
                status_code=404,
                detail=(
                    f"Profile {body.profile_id!r} not found. GET /api/profile "
                    "returns the profile Hermes has stored."
                ),
            )
        params["profile_id"] = str(body.profile_id)
    elif _latest_profile_id(db) is None:
        raise HTTPException(
            status_code=409,
            detail=(
                "No LinkedIn profile has been imported yet, so there is nothing to "
                "build a resume from. POST /api/profile/import first."
            ),
        )

    if body.target_job_id:
        if db.get(Job, coerce_pk(Job, body.target_job_id)) is None:
            raise HTTPException(
                status_code=404,
                detail=(
                    f"Target job {body.target_job_id!r} not found. Run a job search "
                    "first (POST /api/jobs/search)."
                ),
            )
        params["target_job_id"] = str(body.target_job_id)

    if body.label:
        params["label"] = body.label

    return _start(db, "resume_build", params)


# --------------------------------------------------------------------------- #
# GET /resumes, GET /resumes/{id}
# --------------------------------------------------------------------------- #


@router.get("/resumes", summary="List generated + uploaded resumes (newest first)")
def list_resumes(
    profile_id: Optional[str] = Query(None, description="Filter to one profile."),
    target_job_id: Optional[str] = Query(None, description="Filter to one job."),
    limit: int = Query(100, ge=1, le=500),
    offset: int = Query(0, ge=0),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    """Resume rows without their markdown bodies (see ``GET /resumes/{id}``)."""
    filters = []
    if profile_id:
        filters.append(Resume.profile_id == str(profile_id))
    if target_job_id:
        filters.append(Resume.target_job_id == str(target_job_id))

    total = int(
        db.execute(
            select(func.count()).select_from(Resume).where(*filters)
        ).scalar()
        or 0
    )
    rows = (
        db.execute(
            select(Resume)
            .where(*filters)
            .order_by(Resume.created_at.desc(), Resume.version.desc(), Resume.id.desc())
            .limit(limit)
            .offset(offset)
        )
        .scalars()
        .all()
    )
    return {
        "items": [_resume_response(row, full=False) for row in rows],
        "total": total,
        "limit": limit,
        "offset": offset,
    }


@router.get("/resumes/{resume_id}", summary="One resume, with its markdown + ATS detail")
def get_resume(resume_id: str, db: Session = Depends(get_db)) -> dict[str, Any]:
    return _resume_response(_get_resume_or_404(db, resume_id), full=True)


# --------------------------------------------------------------------------- #
# GET /resumes/{id}/download
# --------------------------------------------------------------------------- #


def _download_basename(resume: Resume) -> str:
    """A human-friendly, filesystem-safe download name."""
    label = (getattr(resume, "label", None) or "").strip()
    version = int(getattr(resume, "version", 1) or 1)
    if label:
        return safe_basename(f"{label}_v{version}", f"hermes_resume_v{version}")
    return safe_basename(f"hermes_resume_v{version}", "hermes_resume")


def _materialise_markdown(resume: Resume) -> Path:
    """Write the markdown column to ``settings.resumes_dir`` and return the path.

    The markdown is stored in the DB (that is the source of truth), so ``fmt=md``
    is always serveable — it just needs a file on disk for ``FileResponse``.
    The file is rewritten whenever it is missing or out of date.
    """
    markdown = getattr(resume, "markdown", "") or ""
    target_dir = settings.resumes_dir
    path = target_dir / f"{_download_basename(resume)}.md"
    try:
        target_dir.mkdir(parents=True, exist_ok=True)
        if not path.is_file() or path.read_text(encoding="utf-8") != markdown:
            path.write_text(markdown, encoding="utf-8", newline="\n")
    except OSError as exc:
        raise HTTPException(
            status_code=500,
            detail=(
                f"Could not write the markdown to {path} ({type(exc).__name__}: {exc}). "
                "Check that the hermes-data volume is mounted and writable."
            ),
        ) from exc
    return path


@router.get("/resumes/{resume_id}/download", summary="Download a rendered resume")
def download_resume(
    resume_id: str,
    fmt: str = Query("docx", description="docx | pdf | txt | md"),
    db: Session = Depends(get_db),
) -> FileResponse:
    """Serve one rendered artefact as a file download."""
    wanted = (fmt or "").strip().lower().lstrip(".")
    if wanted not in _MEDIA_TYPES:
        raise HTTPException(
            status_code=422,
            detail=(
                f"Unknown format {fmt!r}. Valid values for ?fmt= are: "
                f"{', '.join(sorted(_MEDIA_TYPES))}."
            ),
        )

    resume = _get_resume_or_404(db, resume_id)
    have = available_formats(resume)

    if wanted == "md":
        if not (getattr(resume, "markdown", "") or "").strip():
            raise HTTPException(
                status_code=404,
                detail=(
                    f"Resume {resume_id} has no markdown body stored, so there is "
                    "nothing to download as .md. Re-run POST /api/resume/generate."
                ),
            )
        path = _materialise_markdown(resume)
    else:
        attr = {"docx": "docx_path", "pdf": "pdf_path", "txt": "txt_path"}[wanted]
        recorded = getattr(resume, attr, None)
        if not recorded:
            raise HTTPException(status_code=404, detail=_missing_format_detail(wanted, have))
        path = Path(str(recorded))
        if not path.is_file():
            raise HTTPException(
                status_code=404,
                detail=(
                    f"The .{wanted} for resume {resume_id} is recorded at {recorded} but "
                    "that file is gone from the data volume. Re-run "
                    "POST /api/resume/generate to render it again"
                    + (f" (available now: {', '.join(have)})." if have else ".")
                ),
            )

    return FileResponse(
        path=str(path),
        media_type=_MEDIA_TYPES[wanted],
        filename=f"{_download_basename(resume)}.{wanted}",
        headers={"Cache-Control": "no-store"},
    )


def _missing_format_detail(wanted: str, have: list[str]) -> str:
    """Explain *why* a format is missing, per format."""
    alternatives = ", ".join(have) if have else "md (from the stored markdown)"
    if wanted == "pdf":
        return (
            "This resume has no PDF. PDF export needs LibreOffice inside the "
            "hermes-sandbox image; when it is unavailable Hermes still produces the "
            ".docx/.txt renders and says so in the run log. Download ?fmt=docx and "
            "export a PDF locally, or rebuild hermes-sandbox with libreoffice-writer "
            f"installed. Available now: {alternatives}."
        )
    if wanted == "docx":
        return (
            "This resume has no .docx. The sandbox rendering step did not complete "
            "(look for 'Rendering failed' in the run log — usually a missing "
            "hermes-sandbox image: run `make build`). The markdown is intact: "
            f"?fmt=md always works. Available now: {alternatives}."
        )
    return (
        f"This resume has no .{wanted}. The sandbox rendering step did not produce it; "
        f"?fmt=md always works. Available now: {alternatives}."
    )


# --------------------------------------------------------------------------- #
# POST /resume/score
# --------------------------------------------------------------------------- #


@router.post("/resume/score", summary="Re-score a resume against the ATS heuristics")
async def score_resume_endpoint(
    payload: ResumeScoreRequest,
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    """Kick off an ``ats_score`` run.

    Pass ``job_id`` to score keyword coverage against that job's description
    instead of the profile keyword bank.

    Honest caveat to surface in the UI: the score is a heuristic proxy for how a
    generic ATS parser is likely to treat the document. It is not any specific
    vendor's parser.
    """
    resume = _get_resume_or_404(db, payload.resume_id)
    if not (getattr(resume, "markdown", "") or "").strip():
        raise HTTPException(
            status_code=409,
            detail=(
                f"Resume {resume.id} has no markdown body to score. Re-run "
                "POST /api/resume/generate."
            ),
        )

    params: dict[str, Any] = {"resume_id": str(resume.id)}
    if payload.job_id:
        if db.get(Job, coerce_pk(Job, payload.job_id)) is None:
            raise HTTPException(
                status_code=404,
                detail=(
                    f"Job {payload.job_id!r} not found. GET /api/jobs lists the jobs "
                    "Hermes has scouted."
                ),
            )
        params["job_id"] = str(payload.job_id)

    return _start(db, "ats_score", params)
