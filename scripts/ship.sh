#!/usr/bin/env bash
# Hermes — build a portable bundle in ./dist for moving to another laptop.
#
# Usage:
#   ./scripts/ship.sh [--mode images|source] [--out DIR] [--force] [--zip]
#
# TWO MODES, AND THE CHOICE IS A REAL TRADEOFF
#
#   --mode images   (default)   "works offline, big and slow to produce"
#       docker save's every image the stack uses into one tar, plus a repo copy.
#       The target needs Docker but NO internet, NO registry access, NO build
#       toolchain. Expect ~3-6 GB and 5-15 minutes to write. Restore is ~2-5
#       minutes of `docker load`. Pick this for an air-gapped or metered
#       machine, or when you want byte-identical images to the ones you tested.
#
#   --mode source               "small and fast, but rebuilds on arrival"
#       Repo copy only, no image tar. ~1-5 MB. The target rebuilds from the
#       Dockerfiles, so it needs internet and 5-15 minutes of build time, and
#       the rebuilt images are not guaranteed byte-identical (upstream base
#       images and package versions move). Pick this on a decent connection.
#
# Never included: .env (holds your freellmapi key), the SQLite DB, node_modules,
# __pycache__, dist. Docker named volumes are NOT in the bundle — use
# scripts/backup.sh for hermes-data and freellmapi-data. The linkedin-session
# volume is deliberately never shipped (see README).

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
. "$SCRIPT_DIR/lib/common.sh"

MODE='images'
OUT_DIR=''
FORCE=0
DO_ZIP=0

while [ $# -gt 0 ]; do
  case "$1" in
    --mode)   MODE="${2:-}"; shift ;;
    --out)    OUT_DIR="${2:-}"; shift ;;
    --force)  FORCE=1 ;;
    --zip)    DO_ZIP=1 ;;
    -h|--help) sed -n '2,30p' "$0"; exit 0 ;;
    *) die "Unknown option: $1 (try --help)" ;;
  esac
  shift
done

case "$MODE" in
  images|source) ;;
  *) die "--mode must be 'images' or 'source' (got '$MODE')" ;;
esac

head_ "HERMES SHIP — mode: $MODE"
cd "$HERMES_ROOT"
assert_docker_ready

if [ -z "$OUT_DIR" ]; then OUT_DIR="$HERMES_ROOT/dist"; fi
REPO_OUT="$OUT_DIR/repo"
TAR_PATH="$OUT_DIR/hermes-images.tar"
PROJECT="$(compose_project_name)"

# ---------------------------------------------------------------------------
# Prepare the output directory
# ---------------------------------------------------------------------------
if [ -d "$OUT_DIR" ] && [ -n "$(ls -A "$OUT_DIR" 2>/dev/null || true)" ]; then
  if [ "$FORCE" -eq 0 ]; then
    warn "$OUT_DIR is not empty."
    if ! ask_yes_no 'Delete its contents and rebuild the bundle?' no; then
      die "Aborted: output directory not empty. Re-run with --force or pass --out <other path>."
    fi
  fi
  step "Clearing $OUT_DIR"
  find "$OUT_DIR" -mindepth 1 -maxdepth 1 -exec rm -rf {} +
fi
mkdir -p "$REPO_OUT"
ok "Bundle directory: $OUT_DIR"

# ---------------------------------------------------------------------------
# Copy the repo (tar-to-tar so excludes are honoured identically everywhere)
# ---------------------------------------------------------------------------
step 'Copying repository'
tar -cf - \
  --exclude='./dist' \
  --exclude='./.git' \
  --exclude='./.env' \
  --exclude='*/node_modules' \
  --exclude='./node_modules' \
  --exclude='*/__pycache__' \
  --exclude='*.pyc' \
  --exclude='*.pyo' \
  --exclude='./data' \
  --exclude='*/.venv' \
  --exclude='./.venv' \
  --exclude='./venv' \
  --exclude='*/.pytest_cache' \
  --exclude='*/.mypy_cache' \
  --exclude='*/.ruff_cache' \
  --exclude='*.log' \
  --exclude='*.tar' \
  --exclude='*.sqlite' \
  --exclude='*.sqlite3' \
  --exclude='*.db' \
  --exclude='*.db-wal' \
  --exclude='*.db-shm' \
  -C "$HERMES_ROOT" . | tar -xf - -C "$REPO_OUT"
ok 'Repository copied (.env, data, node_modules, __pycache__ excluded)'

# Paranoia: prove .env did not slip through.
if [ -f "$REPO_OUT/.env" ]; then
  rm -f "$REPO_OUT/.env"
  warn 'Removed a .env that slipped into the bundle.'
fi

# Compose derives built image names from the project directory basename, so the
# target must match it or set COMPOSE_PROJECT_NAME. load.sh reads this file.
printf '%s' "$PROJECT" > "$REPO_OUT/.hermes-project-name"
ok "Recorded compose project name: $PROJECT"

# ---------------------------------------------------------------------------
# Images
# ---------------------------------------------------------------------------
IMAGES=''
if [ "$MODE" = 'images' ]; then
  step 'Resolving the image list'
  IMAGES="$(compose_images)"
  printf '%s\n' "$IMAGES" | while IFS= read -r i; do [ -n "$i" ] && info "$i"; done

  step 'Verifying every image exists locally'
  MISSING=''
  while IFS= read -r img; do
    [ -z "$img" ] && continue
    if ! docker image inspect "$img" --format '{{.Id}}' >/dev/null 2>&1; then
      MISSING="$MISSING $img"
    fi
  done <<< "$IMAGES"

  if [ -n "$(printf '%s' "$MISSING" | tr -d ' ')" ]; then
    printf '\n'
    for m in $MISSING; do bad "missing image: $m"; done
    info 'Build/pull them first:'
    info '    docker compose build'
    info '    docker compose --profile build-only build'
    info '    docker compose pull'
    info 'Or ship source-only:  ./scripts/ship.sh --mode source'
    die 'Cannot save images: some are missing.'
  fi
  IMG_COUNT="$(printf '%s\n' "$IMAGES" | awk 'NF' | wc -l | tr -d ' ')"
  ok "All ${IMG_COUNT} images present"

  step "Writing $TAR_PATH  (this is the slow part — several GB)"
  # shellcheck disable=SC2086
  docker save -o "$TAR_PATH" $(printf '%s\n' "$IMAGES" | awk 'NF' | tr '\n' ' ')

  TAR_BYTES="$(wc -c < "$TAR_PATH" | tr -d ' ')"
  ok "Image tar written: $(human_bytes "$TAR_BYTES")"

  printf '%s\n' "$IMAGES" | awk 'NF' > "$OUT_DIR/IMAGES.txt"
else
  warn 'Source-only mode: no image tar. The target machine will rebuild from the Dockerfiles.'
  IMAGES="$(compose_images)"
fi

# ---------------------------------------------------------------------------
# RESTORE.txt
# ---------------------------------------------------------------------------
step 'Writing RESTORE.txt'

STAMP="$(date '+%Y-%m-%d %H:%M:%S')"
HOSTNAME_STR="$(hostname 2>/dev/null || printf 'unknown')"
IMG_LIST="$(printf '%s\n' "$IMAGES" | awk 'NF' | sed 's/^/    /')"

{
cat <<EOF
=============================================================================
 HERMES — RESTORE INSTRUCTIONS
 bundle built: ${STAMP}
 mode:         ${MODE}
 source host:  ${HOSTNAME_STR}
 project name: ${PROJECT}
=============================================================================

EOF

if [ "$MODE" = 'images' ]; then
cat <<EOF
THIS BUNDLE CONTAINS PREBUILT IMAGES
    hermes-images.tar   every image the stack uses, exactly as tested
    IMAGES.txt          the image list
    repo/               source, compose file, scripts, docs

The target machine needs Docker. It does NOT need internet access, registry
credentials, or a build toolchain.

STEPS ON THE NEW MACHINE
------------------------
 1. Install Docker Desktop (Windows/macOS) or Docker Engine + Compose v2
    (Linux). Start it and wait until the engine is actually up.

 2. Copy this whole bundle directory to the new machine.

 3. Put the repo somewhere permanent, KEEPING THE DIRECTORY NAME:

        ~/hermes-linkedin-applier

    THE DIRECTORY NAME MATTERS. Compose names locally built images
    <project>-<service>, and the project name defaults to the directory
    basename. This bundle was built with project name:

        ${PROJECT}

    If you rename the directory, Compose looks for images that do not exist
    and tries to rebuild. scripts/load.sh detects this and writes
    COMPOSE_PROJECT_NAME into .env for you.

 4. Load the images (2-5 minutes):

        cd <repo>
        ./scripts/load.sh

    load.sh finds hermes-images.tar next to the repo, runs \`docker load\`,
    creates .env, generates ENCRYPTION_KEY, and brings the stack up WITHOUT
    rebuilding.

    Manual equivalent:
        docker load -i ../hermes-images.tar
        cp .env.example .env
        # set ENCRYPTION_KEY to 64 random hex chars: openssl rand -hex 32
        docker compose up -d --no-build

 5. Do the manual steps. They are per-machine and cannot be copied:

    a. freellmapi key — open http://localhost:3001 , create the local
       account, add free provider keys (Google AI Studio, Groq, Cerebras,
       OpenRouter, Mistral, GitHub Models), copy the freellmapi-... key,
       paste it into .env as FREELLMAPI_KEY, then:
           docker compose up -d hermes-core

       If the browser cannot self-authorise (e.g. you opened it from another
       device), the one-time setup code is in:
           docker compose logs freellmapi

    b. LinkedIn login — run:
           ./scripts/linkedin-login.sh
       then open http://localhost:6080/vnc.html and sign in BY HAND
       (password, 2FA, captcha). This must be redone on every machine.

 6. Open Hermes:  http://localhost:3000
EOF
else
cat <<EOF
THIS BUNDLE IS SOURCE-ONLY
    repo/               source, compose file, scripts, docs
    (no image tar)

The target machine needs Docker AND internet access: it pulls the two upstream
images and builds hermes-core, hermes-dashboard and hermes-sandbox from the
Dockerfiles. Budget 5-15 minutes for the first build.

Tradeoff vs. the images bundle: this is ~1000x smaller, but it rebuilds on
arrival, needs a network, and the resulting images are not guaranteed
byte-identical to the ones tested here (upstream base images and package
versions move on). If that matters, re-run with --mode images.

STEPS ON THE NEW MACHINE
------------------------
 1. Install Docker Desktop (Windows/macOS) or Docker Engine + Compose v2
    (Linux). Start it and wait until the engine is actually up.

 2. Copy repo/ to a permanent location, e.g.  ~/hermes-linkedin-applier

 3. Build and start everything:

        cd <repo>
        ./scripts/bootstrap.sh

    bootstrap.sh runs preflight, creates .env, generates ENCRYPTION_KEY,
    builds all images (including the build-only sandbox image), and starts
    the stack.

 4. Do the manual steps. They are per-machine and cannot be copied:

    a. freellmapi key — open http://localhost:3001 , create the local
       account, add free provider keys (Google AI Studio, Groq, Cerebras,
       OpenRouter, Mistral, GitHub Models), copy the freellmapi-... key,
       paste it into .env as FREELLMAPI_KEY, then:
           docker compose up -d hermes-core

       If the browser cannot self-authorise, the one-time setup code is in:
           docker compose logs freellmapi

    b. LinkedIn login — run:
           ./scripts/linkedin-login.sh
       then open http://localhost:6080/vnc.html and sign in BY HAND
       (password, 2FA, captcha). This must be redone on every machine.

 5. Open Hermes:  http://localhost:3000

Images this project uses (for reference):
${IMG_LIST}
EOF
fi

cat <<EOF

-----------------------------------------------------------------------------
WHAT IS DELIBERATELY NOT IN THIS BUNDLE
-----------------------------------------------------------------------------

 .env
     Holds your FREELLMAPI_KEY and ENCRYPTION_KEY. Secrets do not travel in a
     bundle you might email to yourself. .env.example is included; recreate
     .env on the target (bootstrap / load do it for you).

 Docker named volumes (your data)
     hermes-data       SQLite DB: profile, resumes, jobs, runs + rendered files
     freellmapi-data   your provider keys and the freellmapi account
     Move these ONLY if you want your history and provider config to come with
     you, and use the dedicated tooling:
         old machine:  ./scripts/backup.sh
         new machine:  ./scripts/restore.sh --from <backup dir>
     If you skip this, Hermes starts empty: re-import the profile, re-add the
     provider keys. That is a perfectly normal way to move.

 linkedin-session          <-- DO NOT COPY THIS ONE
     A live, authenticated LinkedIn browser profile: cookies, tokens, device
     fingerprint. Copying it between machines is
       (a) fragile  — the fingerprint no longer matches the new host, so
                      LinkedIn is more likely to invalidate the session or
                      flag the account, and
       (b) a security risk — those files are bearer credentials to your
                      LinkedIn account, in plain form, on disk and in every
                      backup you keep.
     Just run scripts/linkedin-login.sh on the new machine and sign in again.
     It takes a minute and is the supported path. backup.sh refuses to
     include it.

-----------------------------------------------------------------------------
TROUBLESHOOTING THE RESTORE
-----------------------------------------------------------------------------

 "compose is rebuilding even though I loaded the images"
     The project name does not match. Set COMPOSE_PROJECT_NAME=${PROJECT} in
     .env, or rename the repo directory, then: docker compose up -d --no-build

 "docker load" says "no space left on device"
     The tar expands. Free at least 3x the tar size, or use --mode source.

 "unauthorized" / 401 from the LLM
     FREELLMAPI_KEY is missing, stale, or from the other machine's freellmapi.
     Keys are per-freellmapi-instance: mint a new one in the new dashboard.

 MCP says "not authenticated"
     Expected on a fresh machine. Run scripts/linkedin-login.sh.

 Ports 3000/3001/8080/6080 in use
     Change them in .env and re-run, or stop whatever owns them.

 Full reference: README.md , docs/RUNBOOK.md
=============================================================================
EOF
} > "$OUT_DIR/RESTORE.txt"

ok "Wrote $OUT_DIR/RESTORE.txt"

# ---------------------------------------------------------------------------
# Optional zip
# ---------------------------------------------------------------------------
if [ "$DO_ZIP" -eq 1 ]; then
  if have zip; then
    step 'Compressing the bundle'
    if [ "$MODE" = 'images' ]; then
      info 'A docker save tar barely compresses (layers are already gzipped); this mostly costs time.'
    fi
    ZIP_PATH="$(dirname "$OUT_DIR")/$(basename "$OUT_DIR")-$(date '+%Y%m%d-%H%M%S').zip"
    ( cd "$OUT_DIR" && zip -qr "$ZIP_PATH" . )
    ok "Zip: $ZIP_PATH ($(human_bytes "$(wc -c < "$ZIP_PATH" | tr -d ' ')"))"
  else
    warn 'zip is not installed; skipping. Copy the bundle folder directly instead.'
  fi
fi

# ---------------------------------------------------------------------------
# Summary
# ---------------------------------------------------------------------------
TOTAL_BYTES="$(du -sk "$OUT_DIR" 2>/dev/null | awk '{print $1*1024}')"
if [ -z "$TOTAL_BYTES" ]; then TOTAL_BYTES=0; fi

head_ 'BUNDLE READY'
printf '  Location: %s\n' "$OUT_DIR"
printf '  Mode:     %s\n' "$MODE"
printf '  Size:     %s\n\n' "$(human_bytes "$TOTAL_BYTES")"
printf '  Next:\n'
printf '    1. Copy this whole folder to the target machine (USB or file share).\n'
if [ "$MODE" = 'images' ]; then
  printf '    2. On the target:  cd <repo> && ./scripts/load.sh\n'
else
  printf '    2. On the target:  cd <repo> && ./scripts/bootstrap.sh\n'
fi
printf '    3. Read RESTORE.txt — it lists the manual per-machine steps.\n\n'
warn 'Your data (SQLite DB, provider keys) is NOT in this bundle.'
info 'If you want it: ./scripts/backup.sh here, ./scripts/restore.sh there.'
info 'Never copy the linkedin-session volume — re-login instead.'
printf '\n'
