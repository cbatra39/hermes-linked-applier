# ---------------------------------------------------------------------------
# Hermes core — FastAPI orchestrator / agent runtime.
#
# ABOUT THE CONTAINER USER (deliberate decision, please read before "fixing"):
#   hermes-core must talk to the Docker Engine API over the bind-mounted
#   /var/run/docker.sock in order to spawn ephemeral `hermes-sandbox`
#   containers and to power the dashboard's Containers page.
#
#   The socket on the host is owned by root and group `docker`, and that GID
#   differs per host (Debian ~999/998, Ubuntu ~999, Docker Desktop / WSL2 may
#   expose it as root:root with mode 0660). To run as a non-root user you would
#   have to bake the *host's* docker GID into the image (build arg + `usermod
#   -aG`), which makes the image non-portable, or relax the socket's mode on
#   the host, which is worse.
#
#   TRADEOFF, stated explicitly: we run as ROOT inside this container. That is
#   acceptable and the simplest correct option here because
#     (a) anything with access to the docker socket is already root-equivalent
#         on the host, so a non-root UID inside the container buys ~nothing,
#     (b) the untrusted work (LLM-authored code, document rendering) never runs
#         in THIS container — it runs in `hermes-sandbox` containers which are
#         hardened: user 1000:1000, cap_drop=ALL, no-new-privileges, read-only
#         rootfs, tmpfs /tmp, pids/mem/cpu limits, network disabled.
#   If you must run non-root, build with:
#       docker build --build-arg DOCKER_GID=$(getent group docker | cut -d: -f3) .
#   and add the usermod/USER lines back; expect it to break on Docker Desktop.
# ---------------------------------------------------------------------------
FROM python:3.12-slim

# Fail loudly and predictably; no .pyc noise on the mounted volume.
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONFAULTHANDLER=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1

# curl -> container HEALTHCHECK below (and handy for debugging the router/MCP).
# ca-certificates -> outbound HTTPS if a provider is reached directly.
RUN apt-get update \
 && apt-get install -y --no-install-recommends curl ca-certificates tini \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Dependency layer first so code edits don't invalidate the pip cache layer.
COPY requirements.txt ./requirements.txt
RUN pip install --no-cache-dir -r requirements.txt

# Application code (single package, no editable install needed).
COPY hermes ./hermes

# Default data root; docker-compose mounts a volume here so the SQLite DB,
# generated resumes and sandbox workspaces survive container replacement.
RUN mkdir -p /data/workspaces /data/resumes /data/uploads
VOLUME ["/data"]

ENV HERMES_DATA_DIR=/data \
    HERMES_SANDBOX_WORKSPACE=/data/workspaces \
    HERMES_API_PORT=8080

EXPOSE 8080

# The API mounts everything under /api (see hermes/main.py).
HEALTHCHECK --interval=30s --timeout=5s --start-period=25s --retries=5 \
    CMD curl -fsS http://127.0.0.1:8080/api/health || exit 1

# tini reaps zombies (the docker SDK + subprocess churn can leave them around).
ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["uvicorn", "hermes.main:app", "--host", "0.0.0.0", "--port", "8080", "--no-server-header"]
