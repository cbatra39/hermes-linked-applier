# =============================================================================
#  hermes-sandbox — the throwaway container Hermes runs untrusted work in.
# =============================================================================
#  NOT A SERVICE. Nothing ever runs this as a long-lived compose service; it is
#  built (see the `hermes-sandbox-image` entry in docker-compose.yml, profile
#  `build-only`) and then hermes-core spawns short-lived containers FROM it
#  through the Docker socket — one per render / per code-exec job.
#
#  Build:   docker compose --profile build-only build     (== `make build`)
#  Tag:     ${HERMES_SANDBOX_IMAGE}, default hermes-linkedin/sandbox:latest
#
#  ---------------------------------------------------------------------------
#  THE RUNTIME CONTRACT THIS IMAGE MUST SATISFY
#  ---------------------------------------------------------------------------
#  services/core/hermes/sandbox.py starts every container with, unconditionally:
#
#      user            = 1000:1000          <- so uid/gid 1000 must EXIST here
#      working_dir     = /work              <- the per-run workspace bind mount
#      command         = python -u /work/main.py [args...]
#      read_only       = True               <- ROOT FILESYSTEM IS READ-ONLY
#      tmpfs           = {"/tmp": "size=64m"}
#      network_disabled= True  (HERMES_SANDBOX_NETWORK=none)
#      cap_drop        = ALL ; security_opt = no-new-privileges:true
#      pids_limit      = 256 ; mem/cpu capped from .env
#      env             HOME=/work  TMPDIR=/tmp  XDG_CACHE_HOME=/tmp/.cache
#                      XDG_CONFIG_HOME=/tmp/.config  PYTHONDONTWRITEBYTECODE=1
#
#  Consequences you must not break:
#    * NOTHING may write outside /work and /tmp at runtime. No /var/log, no
#      $HOME under /home, no font-cache rebuild into /usr/share, no pip.
#      Anything that tries gets EROFS and the run fails.
#    * The font cache is therefore built HERE, at image build time (fc-cache
#      below), so fontconfig never needs to write at runtime.
#    * uid 1000 must be able to READ /opt/hermes/ats_docx.py and the whole
#      Python install. It never needs to write to either.
#    * /work is only a mount point in the image; the real content is the
#      per-run named-volume subdirectory Docker binds over it.
#
#  ---------------------------------------------------------------------------
#  SIZE WARNING — LIBREOFFICE IS ~500-700 MB OF THIS IMAGE
#  ---------------------------------------------------------------------------
#  Without LibreOffice this image is ~180 MB. With libreoffice-writer-nogui and
#  its dependency closure it is ~700 MB - 1.0 GB. LibreOffice buys exactly one
#  feature: the optional .docx -> .pdf conversion in ats_docx.py. The .docx and
#  .txt outputs — the ones an ATS actually ingests — do not need it at all.
#
#  TO SLIM THE IMAGE BY DROPPING PDF SUPPORT (saves ~500-800 MB):
#
#      docker compose --profile build-only build \
#          --build-arg INCLUDE_PDF=0 hermes-sandbox-image
#
#      # or, without compose:
#      docker build --build-arg INCLUDE_PDF=0 -t hermes-linkedin/sandbox:latest \
#          ./services/sandbox
#
#  With INCLUDE_PDF=0 the LibreOffice apt step is skipped entirely. Nothing
#  breaks: ats_docx.py's find_soffice() returns None, build_pdf() returns
#  (None, "LibreOffice (soffice) is not installed in the sandbox image; PDF
#  skipped."), the render still produces .docx + .txt, and the dashboard's
#  download menu simply does not offer `pdf`. If you later want PDFs back,
#  rebuild without the build arg — no code or config change is needed.
#
#  (Do NOT try to slim this by installing LibreOffice into hermes-core instead.
#  Document conversion is the untrusted step; it belongs in the sandbox.)
# =============================================================================

FROM python:3.12-slim

# Set to 0 to build without LibreOffice — see the SIZE WARNING above.
ARG INCLUDE_PDF=1

# Pinned so a rebuild six months from now produces the same document output.
# python-docx is the ONLY hard dependency of ats_docx.py. Keep it that way:
# the sandbox has no network at runtime, so nothing can be installed later.
# (Matches the pin in services/core/requirements.txt so hermes-core and the
# sandbox agree on .docx generation behaviour.)
ARG PYTHON_DOCX_VERSION=1.1.2

LABEL org.opencontainers.image.title="hermes-sandbox" \
      org.opencontainers.image.description="Ephemeral, network-disabled worker image for Hermes: ATS-safe document rendering and untrusted Python execution." \
      com.hermes.stack="hermes" \
      com.hermes.role="sandbox-image"

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONFAULTHANDLER=1 \
    PYTHONIOENCODING=utf-8 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1 \
    LANG=C.UTF-8 \
    LC_ALL=C.UTF-8

# SAL_USE_VCLPLUGIN=svp selects LibreOffice's headless "server" VCL backend, so
# it never looks for X11/GTK. sandbox.py sets HOME/TMPDIR/XDG_* on every run;
# these defaults exist so `docker run hermes-sandbox ...` by hand behaves the
# same way (both point writes at /work and /tmp, the only writable paths).
ENV SAL_USE_VCLPLUGIN=svp \
    HOME=/work \
    TMPDIR=/tmp

# -----------------------------------------------------------------------------
# System packages
# -----------------------------------------------------------------------------
#  fonts-liberation  Liberation Sans/Serif/Mono are metric-compatible with
#                    Arial / Times New Roman / Courier New. ats_docx.py writes
#                    Calibri/Arial font names into the .docx; without these the
#                    PDF renders in a fallback face with different metrics and
#                    the page count drifts. Also needed for the .docx itself to
#                    look sane if anyone previews it inside the container.
#  fontconfig        provides fc-cache, used below to pre-build the font cache.
#
#  LibreOffice: prefer libreoffice-writer-nogui (Writer without the GUI stack —
#  significantly smaller). It only exists on Debian bookworm and newer; on
#  older/derived bases the name is absent, so fall back to libreoffice-writer.
#  Both pull libreoffice-core, which is where the actual PDF export lives.
#  --no-install-recommends everywhere: recommends drag in Java, dictionaries,
#  and the whole KDE/GNOME integration for no benefit to a headless converter.
# -----------------------------------------------------------------------------
RUN set -eux; \
    apt-get update; \
    apt-get install -y --no-install-recommends \
        fonts-liberation \
        fontconfig \
    ; \
    if [ "$INCLUDE_PDF" = "1" ]; then \
        echo "[hermes-sandbox] installing LibreOffice for optional PDF export (~500MB+)"; \
        if apt-get install -y --no-install-recommends libreoffice-writer-nogui; then \
            echo "[hermes-sandbox] installed libreoffice-writer-nogui"; \
        else \
            echo "[hermes-sandbox] libreoffice-writer-nogui unavailable on this base; falling back to libreoffice-writer"; \
            apt-get install -y --no-install-recommends libreoffice-writer; \
        fi; \
    else \
        echo "[hermes-sandbox] INCLUDE_PDF=0 -> LibreOffice NOT installed; ats_docx.py will skip PDF and still emit .docx + .txt"; \
    fi; \
    rm -rf /var/lib/apt/lists/*

# python-docx: the .docx writer used by ats_docx.py.
RUN pip install --no-cache-dir "python-docx==${PYTHON_DOCX_VERSION}"

# -----------------------------------------------------------------------------
# Non-root user, uid/gid 1000 — sandbox.py hard-codes user="1000:1000".
# -----------------------------------------------------------------------------
# Debian's python:3.12-slim has no uid 1000, but a future base might, so this is
# written idempotently rather than assuming. /home/hermes exists mainly so the
# account is well-formed; at run time HOME points at /work, because /home is on
# the read-only root filesystem.
RUN set -eux; \
    if ! getent group 1000 >/dev/null; then groupadd --gid 1000 hermes; fi; \
    if ! getent passwd 1000 >/dev/null; then \
        useradd --uid 1000 --gid 1000 --create-home --home-dir /home/hermes \
                --shell /usr/sbin/nologin hermes; \
    fi; \
    mkdir -p /work /opt/hermes; \
    chown 1000:1000 /work

# -----------------------------------------------------------------------------
# The renderer.
# -----------------------------------------------------------------------------
# hermes-core normally INJECTS its own copy of ats_docx.py into the workspace
# (see render.py: it is written to /work/ats_docx.py). The copy baked in here at
# /opt/hermes/ats_docx.py is the fallback the in-sandbox driver falls back to —
# render.py's IMAGE_SCRIPT_PATH is exactly "/opt/hermes/ats_docx.py", so do not
# move or rename it. Mode 0644: readable by uid 1000, writable by nobody.
#
# `COPY --chmod=` is deliberately NOT used: it requires BuildKit, and this must
# also build with the legacy builder (DOCKER_BUILDKIT=0), which some corporate
# Docker installs still pin.
COPY ats_docx.py /opt/hermes/ats_docx.py
RUN chmod 0644 /opt/hermes/ats_docx.py && chown root:root /opt/hermes/ats_docx.py

# -----------------------------------------------------------------------------
# Pre-build the fontconfig cache.
# -----------------------------------------------------------------------------
# At run time the root filesystem is read-only and XDG_CACHE_HOME is a 64 MB
# tmpfs, so LibreOffice must not have to scan and cache fonts on first use.
# Doing it here makes the first (and only) conversion in each ephemeral
# container several seconds faster and removes a write that would otherwise
# fail on a smaller tmpfs.
RUN set -eux; fc-cache --force --system-only >/dev/null 2>&1 || fc-cache --force || true

# -----------------------------------------------------------------------------
# Verify the image can actually do its job — at BUILD time, not at 2am.
# -----------------------------------------------------------------------------
# A broken pin or a base-image change should fail `make build`, not surface as a
# cryptic failed run three days later. This does not exercise LibreOffice (that
# needs a real document and ~10s); it only proves the imports resolve and the
# renderer's CLI parses.
RUN set -eux; \
    python -c "import docx; print('python-docx', docx.__version__ if hasattr(docx, '__version__') else 'ok')"; \
    python /opt/hermes/ats_docx.py --help >/dev/null; \
    if [ "$INCLUDE_PDF" = "1" ]; then \
        (command -v soffice >/dev/null || command -v libreoffice >/dev/null) \
          || { echo "ERROR: INCLUDE_PDF=1 but no soffice/libreoffice binary is on PATH" >&2; exit 1; }; \
    fi

WORKDIR /work
USER 1000:1000

# The base image declares no healthcheck; make that explicit. These containers
# live for seconds and are supervised by hermes-core, which reads the exit code
# and the workspace, so a healthcheck would only add noise to `docker ps`.
HEALTHCHECK NONE

# sandbox.py always passes an explicit command ("python -u /work/main.py ...").
# This default only matters for a human poking at the image by hand, e.g.:
#   docker run --rm -it -v "$PWD:/work" -u 1000:1000 hermes-linkedin/sandbox:latest \
#       python /opt/hermes/ats_docx.py --input /work/resume.md --outdir /work
CMD ["python"]
