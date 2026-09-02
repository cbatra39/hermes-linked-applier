#!/usr/bin/env bash
# Hermes — the receiving half of `ship`: docker load the image tar, verify the
# stack's images are all present, then bring everything up WITHOUT rebuilding.
#
# Usage:
#   ./scripts/load.sh [--tar PATH] [--project NAME] [--skip-load] [--all] [--no-up]
#
#   --tar PATH      Explicit path to hermes-images.tar. Default: searched in
#                   ./dist, ., ../ and ../dist (the layout ship.sh produces).
#   --project NAME  Override the compose project name.
#   --skip-load     Skip `docker load`; verify + start only.
#   --all           Start hermes-core even if FREELLMAPI_KEY is still blank.
#   --no-up         Load and verify only; start nothing.
#
# Run this on the TARGET machine, from inside the unpacked repo.
#
# Safe to re-run: `docker load` on already-present layers is a no-op.
#
# This does NOT restore your data. The SQLite DB and the freellmapi provider
# keys live in Docker volumes, which are not part of a ship bundle — use
# scripts/backup.sh on the old machine and scripts/restore.sh here. The LinkedIn
# session is never transferred: re-run scripts/linkedin-login.sh.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
. "$SCRIPT_DIR/lib/common.sh"

TAR_PATH=''
PROJECT_OVERRIDE=''
SKIP_LOAD=0
START_ALL=0
NO_UP=0

while [ $# -gt 0 ]; do
  case "$1" in
    --tar)       TAR_PATH="${2:-}"; shift ;;
    --project)   PROJECT_OVERRIDE="${2:-}"; shift ;;
    --skip-load) SKIP_LOAD=1 ;;
    --all)       START_ALL=1 ;;
    --no-up)     NO_UP=1 ;;
    -h|--help)   sed -n '2,25p' "$0"; exit 0 ;;
    *) die "Unknown option: $1 (try --help)" ;;
  esac
  shift
done

# ---------------------------------------------------------------------------
# Local helper: the REAL compose project name.
#
# docker-compose.yml pins `name: hermes` at the top level, and that beats the
# directory basename that compose_project_name() falls back to. Precedence, per
# the Compose Specification: -p > COMPOSE_PROJECT_NAME > `name:` in the compose
# file > directory basename. Named volumes are prefixed with the winner, so
# getting this wrong means hunting for volumes that do not exist.
# ---------------------------------------------------------------------------
hermes_project() {
  if [ -n "$PROJECT_OVERRIDE" ]; then printf '%s\n' "$PROJECT_OVERRIDE"; return 0; fi
  if [ -n "${COMPOSE_PROJECT_NAME:-}" ]; then printf '%s\n' "$COMPOSE_PROJECT_NAME"; return 0; fi

  local from_env; from_env="$(dotenv_get COMPOSE_PROJECT_NAME '')"
  if [ -n "$from_env" ]; then printf '%s\n' "$from_env"; return 0; fi

  if [ -f "$HERMES_ROOT/docker-compose.yml" ]; then
    local from_yaml
    from_yaml="$(sed -n -E 's/^name:[[:space:]]*([A-Za-z0-9][A-Za-z0-9_-]*)[[:space:]]*$/\1/p' \
                  "$HERMES_ROOT/docker-compose.yml" | head -n1)"
    if [ -n "$from_yaml" ]; then printf '%s\n' "$from_yaml"; return 0; fi
  fi

  compose_project_name
}

head_ 'HERMES LOAD — offline install from an image tar'
cd "$HERMES_ROOT"
assert_docker_ready

# ---------------------------------------------------------------------------
# 1. Locate the image tar
# ---------------------------------------------------------------------------
if [ "$SKIP_LOAD" -eq 0 ]; then
  if [ -z "$TAR_PATH" ]; then
    step 'Looking for hermes-images.tar'
    PARENT="$(cd "$HERMES_ROOT/.." && pwd)"
    for c in "$HERMES_ROOT/dist/hermes-images.tar" \
             "$HERMES_ROOT/hermes-images.tar" \
             "$PARENT/hermes-images.tar" \
             "$PARENT/dist/hermes-images.tar"; do
      info "checking $c"
      if [ -f "$c" ]; then TAR_PATH="$c"; break; fi
    done
  fi

  if [ -z "$TAR_PATH" ] || [ ! -f "$TAR_PATH" ]; then
    printf '\n'
    info 'Searched:'
    info '    ./dist/hermes-images.tar'
    info '    ./hermes-images.tar'
    info '    ../hermes-images.tar          (the layout scripts/ship.sh produces)'
    info '    ../dist/hermes-images.tar'
    printf '\n'
    info 'Either point at it explicitly:'
    info '    ./scripts/load.sh --tar <path to hermes-images.tar>'
    info 'or, if you have no bundle, build from source instead:'
    info '    ./scripts/bootstrap.sh'
    die 'hermes-images.tar not found.'
  fi

  TAR_BYTES="$(wc -c < "$TAR_PATH" | tr -d ' ')"
  ok "Image tar: $TAR_PATH  ($(human_bytes "$TAR_BYTES"))"
else
  warn 'Skipping docker load (--skip-load).'
fi

# ---------------------------------------------------------------------------
# 2. .env (needed before the project-name check, which reads it)
# ---------------------------------------------------------------------------
step 'Preparing .env'
if [ ! -f "$HERMES_ENV_FILE" ]; then
  if [ -f "$HERMES_ENV_EXAMPLE" ]; then
    cp "$HERMES_ENV_EXAMPLE" "$HERMES_ENV_FILE"
    ok 'Created .env from .env.example'
  else
    warn '.env.example is missing from this checkout — writing the built-in default instead.'
    env_example_fallback > "$HERMES_ENV_FILE"
    ok 'Created .env from the built-in template'
  fi
else
  ok '.env already exists (left untouched except for a blank ENCRYPTION_KEY)'
fi

ENC_KEY="$(dotenv_get ENCRYPTION_KEY '')"
if [ -z "$ENC_KEY" ]; then
  dotenv_set ENCRYPTION_KEY "$(new_hex_key 32)"
  ok 'Generated a new 64-char hex ENCRYPTION_KEY into .env'
  info 'If you are ALSO restoring the freellmapi-data volume from another machine,'
  info 'that volume was encrypted with the OTHER machine ENCRYPTION_KEY. Copy that'
  info 'value into .env instead, or re-add the provider keys by hand.'
elif [ "${#ENC_KEY}" -ne 64 ]; then
  warn "ENCRYPTION_KEY is ${#ENC_KEY} chars; freellmapi expects 64 hex characters. Leaving it as-is."
else
  ok 'ENCRYPTION_KEY already set'
fi

# ---------------------------------------------------------------------------
# 3. Compose project name alignment
# ---------------------------------------------------------------------------
PROJECT="$(hermes_project)"
ok "Compose project name: $PROJECT"

if [ -f "$HERMES_ROOT/.hermes-project-name" ]; then
  SHIPPED="$(tr -d '\r\n' < "$HERMES_ROOT/.hermes-project-name")"
  if [ -n "$SHIPPED" ] && [ "$SHIPPED" != "$PROJECT" ]; then
    warn "This bundle was built with compose project name \"$SHIPPED\", this machine resolves to \"$PROJECT\"."
    info 'Docker prefixes named volumes with the project name, so the two would use'
    info 'different volumes. Pinning COMPOSE_PROJECT_NAME in .env to match the bundle'
    info 'so backups taken on the other machine restore into the right place.'
    dotenv_set COMPOSE_PROJECT_NAME "$SHIPPED"
    export COMPOSE_PROJECT_NAME="$SHIPPED"
    PROJECT="$SHIPPED"
    ok "COMPOSE_PROJECT_NAME=$SHIPPED written to .env"
  fi
fi
if [ -n "$PROJECT_OVERRIDE" ]; then export COMPOSE_PROJECT_NAME="$PROJECT_OVERRIDE"; fi

# ---------------------------------------------------------------------------
# 4. docker load
# ---------------------------------------------------------------------------
if [ "$SKIP_LOAD" -eq 0 ]; then
  step 'docker load — this takes 2-5 minutes and is disk-bound'
  info 'The tar expands as it loads. Make sure you have ~3x its size free.'
  docker load -i "$TAR_PATH"
  ok 'Images loaded'
fi

# ---------------------------------------------------------------------------
# 5. Verify the images
# ---------------------------------------------------------------------------
step "Verifying the images the compose file needs"

# Prefer the manifest ship wrote next to the tar; it is the exact list that was
# saved. Fall back to asking compose.
EXPECTED=''
if [ -n "$TAR_PATH" ] && [ -f "$(dirname "$TAR_PATH")/IMAGES.txt" ]; then
  EXPECTED="$(awk 'NF' "$(dirname "$TAR_PATH")/IMAGES.txt")"
  if [ -n "$EXPECTED" ]; then info "using the bundle manifest: $(dirname "$TAR_PATH")/IMAGES.txt"; fi
fi
if [ -z "$EXPECTED" ]; then EXPECTED="$(compose_images)"; fi

MISSING=''
EXPECTED_COUNT=0
while IFS= read -r img; do
  [ -z "$img" ] && continue
  EXPECTED_COUNT=$((EXPECTED_COUNT + 1))
  if ID="$(docker image inspect "$img" --format '{{.Id}}' 2>/dev/null)"; then
    ok "$img ${ID:0:19}"
  else
    bad "MISSING  $img"
    MISSING="$MISSING $img"
  fi
done <<< "$EXPECTED"

if [ -n "$(printf '%s' "$MISSING" | tr -d ' ')" ]; then
  printf '\n'
  info 'A complete offline Hermes needs all five images:'
  info '    hermes-core:latest                         (built here)'
  info '    hermes-dashboard:latest                    (built here)'
  info '    hermes-linkedin/sandbox:latest                      (built here, profile build-only)'
  info '    ghcr.io/tashfeenahmed/freellmapi:latest    (pulled)'
  info '    stickerdaniel/linkedin-mcp-server:4.23.2   (pulled)'
  printf '\n'
  info 'Fix options:'
  info '  a) the bundle was made with --mode source (no tar): run ./scripts/bootstrap.sh'
  info '  b) the bundle is incomplete: re-run ./scripts/ship.sh --mode images on the source machine'
  info '  c) you have internet: docker compose --profile build-only build && docker compose pull'
  die 'Some images are still missing after load; the stack cannot start offline.'
fi
ok "All ${EXPECTED_COUNT} images present"

if [ "$NO_UP" -eq 1 ]; then
  warn 'Stopping here (--no-up). Start the stack with:  docker compose up -d --no-build'
  exit 0
fi

# ---------------------------------------------------------------------------
# 6. Up — explicitly --no-build, the whole point of loading
# ---------------------------------------------------------------------------
FREE_KEY="$(dotenv_get FREELLMAPI_KEY '')"
CORE_DEFERRED=0

if [ -z "$FREE_KEY" ] && [ "$START_ALL" -eq 0 ]; then
  CORE_DEFERRED=1
  step 'Starting freellmapi, linkedin-mcp and hermes-dashboard'
  info 'hermes-core is held back: FREELLMAPI_KEY is blank, so it has no LLM to talk to.'
  docker compose up -d --no-build freellmapi linkedin-mcp hermes-dashboard
else
  step 'Starting the stack (no build)'
  docker compose up -d --no-build
fi

step 'Container status'
docker compose ps || true

# ---------------------------------------------------------------------------
# 7. Handover
# ---------------------------------------------------------------------------
PORT_DASH="$(dotenv_get HERMES_DASHBOARD_PORT 3000)"
PORT_LLM="$(dotenv_get FREELLMAPI_PORT 3001)"
PORT_API="$(dotenv_get HERMES_API_PORT 8080)"
HOST_BIND_VAL="$(dotenv_get HOST_BIND 127.0.0.1)"
PROBE_HOST="$HOST_BIND_VAL"
if [ "$PROBE_HOST" = '0.0.0.0' ] || [ -z "$PROBE_HOST" ]; then PROBE_HOST='127.0.0.1'; fi

wait_http_ok "http://$PROBE_HOST:$PORT_LLM/api/ping" 90 'freellmapi' || true
wait_http_ok "http://$PROBE_HOST:$PORT_DASH/" 60 'dashboard' || true
if [ "$CORE_DEFERRED" -eq 0 ]; then
  wait_http_ok "http://$PROBE_HOST:$PORT_API/api/health" 90 'hermes-core' || true
fi

head_ 'LOADED — NOW THE PARTS ONLY YOU CAN DO'
cat <<EOF

  (1) MINT A NEW freellmapi KEY ON THIS MACHINE
      Open  http://$PROBE_HOST:$PORT_LLM
      Create the local account, add free provider keys, copy the
      freellmapi-... token into .env as FREELLMAPI_KEY, then:
          docker compose up -d --no-build hermes-core
      Tokens are per-instance: the old machine key will NOT work here.
      If the browser cannot self-authorise (you opened it from another device),
      the one-time setup code is printed in:  docker compose logs freellmapi

  (2) LOG IN TO LINKEDIN, BY HAND, ON THIS MACHINE
          ./scripts/linkedin-login.sh
      The session is never shipped or restored. This is deliberate: it is a live
      authenticated browser profile, and copying it is both fragile and a
      credential leak.

  (3) OPTIONAL — BRING YOUR DATA OVER
      On the old machine:  ./scripts/backup.sh
      Here:                ./scripts/restore.sh --from <backup dir>
      Skip this and Hermes just starts empty; re-import the profile.

  (4) OPEN HERMES:  http://$PROBE_HOST:$PORT_DASH

  Hermes never submits an application for you. It ranks jobs, tailors a resume,
  and hands you an apply link. You click it.

EOF

if [ "$CORE_DEFERRED" -eq 1 ]; then
  warn 'hermes-core is NOT running yet. Finish step (1), then: docker compose up -d --no-build hermes-core'
fi
info 'Docs: README.md  |  docs/ARCHITECTURE.md  |  docs/RUNBOOK.md'
printf '\n'
