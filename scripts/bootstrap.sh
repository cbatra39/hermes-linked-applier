#!/usr/bin/env bash
# Hermes — one-shot bootstrap: preflight, .env, build, up, then tell the human
# exactly which steps only they can do.
#
# Usage:
#   ./scripts/bootstrap.sh [--skip-build] [--skip-preflight] [--force]
#                          [--all] [--pull] [--no-cache]
#
#   --skip-build      Skip both build steps (images already built or loaded).
#   --skip-preflight  Skip Docker/port checks. Not recommended.
#   --force           Port conflicts become warnings instead of hard failures.
#   --all             Start hermes-core even if FREELLMAPI_KEY is still blank.
#   --pull            Pull the upstream images first.
#   --no-cache        Build without the layer cache.
#
# Safe to re-run. Nothing here is destructive.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
. "$SCRIPT_DIR/lib/common.sh"

SKIP_BUILD=0
SKIP_PREFLIGHT=0
FORCE=0
START_ALL=0
DO_PULL=0
NO_CACHE=0

while [ $# -gt 0 ]; do
  case "$1" in
    --skip-build)     SKIP_BUILD=1 ;;
    --skip-preflight) SKIP_PREFLIGHT=1 ;;
    --force)          FORCE=1 ;;
    --all)            START_ALL=1 ;;
    --pull)           DO_PULL=1 ;;
    --no-cache)       NO_CACHE=1 ;;
    -h|--help)        sed -n '2,20p' "$0"; exit 0 ;;
    *) die "Unknown option: $1 (try --help)" ;;
  esac
  shift
done

head_ 'HERMES BOOTSTRAP'
info "Project root: $HERMES_ROOT"
cd "$HERMES_ROOT"

# ---------------------------------------------------------------------------
# 1. PREFLIGHT
# ---------------------------------------------------------------------------

COMPOSE_FILE=''
for c in docker-compose.yml docker-compose.yaml compose.yml compose.yaml; do
  if [ -f "$HERMES_ROOT/$c" ]; then COMPOSE_FILE="$c"; break; fi
done
if [ -z "$COMPOSE_FILE" ]; then
  die "No compose file found in $HERMES_ROOT. Expected docker-compose.yml. Are you running this from a complete Hermes checkout?"
fi
ok "Compose file: $COMPOSE_FILE"

if [ "$SKIP_PREFLIGHT" -eq 0 ]; then
  assert_docker_ready
else
  warn 'Preflight skipped (--skip-preflight).'
  have docker || die 'docker is not on PATH; even with --skip-preflight this cannot continue.'
fi

# ---------------------------------------------------------------------------
# 2. CONFIG (.env)
# ---------------------------------------------------------------------------

step 'Preparing .env'

if [ ! -f "$HERMES_ENV_FILE" ]; then
  if [ -f "$HERMES_ENV_EXAMPLE" ]; then
    cp "$HERMES_ENV_EXAMPLE" "$HERMES_ENV_FILE"
    ok 'Created .env from .env.example'
  else
    warn '.env.example is missing from this checkout — writing a built-in default .env instead.'
    env_example_fallback > "$HERMES_ENV_FILE"
    ok 'Created .env from the built-in template'
  fi
else
  ok '.env already exists (left untouched except for a blank ENCRYPTION_KEY)'
fi

# freellmapi encrypts stored provider keys with ENCRYPTION_KEY. Blank => it will
# refuse to start or will store secrets in the clear.
ENC_KEY="$(dotenv_get ENCRYPTION_KEY '')"
if [ -z "$ENC_KEY" ]; then
  NEW_KEY="$(new_hex_key 32)"
  dotenv_set ENCRYPTION_KEY "$NEW_KEY"
  ok 'Generated a new 64-char hex ENCRYPTION_KEY into .env'
  info 'Back this up. Losing it makes the provider keys stored inside freellmapi unreadable.'
else
  if [ "${#ENC_KEY}" -ne 64 ]; then
    warn "ENCRYPTION_KEY is ${#ENC_KEY} chars; freellmapi expects 64 hex characters. Leaving it as-is."
  else
    ok 'ENCRYPTION_KEY already set'
  fi
fi

PORT_DASH="$(dotenv_get HERMES_DASHBOARD_PORT 3000)"
PORT_LLM="$(dotenv_get FREELLMAPI_PORT 3001)"
PORT_API="$(dotenv_get HERMES_API_PORT 8080)"
PORT_VIEWER="$(dotenv_get LINKEDIN_VIEWER_PORT 6080)"
HOST_BIND_VAL="$(dotenv_get HOST_BIND 127.0.0.1)"
FREE_KEY="$(dotenv_get FREELLMAPI_KEY '')"

# ---------------------------------------------------------------------------
# Ports (checked after .env so we test the configured ports)
# ---------------------------------------------------------------------------

if [ "$SKIP_PREFLIGHT" -eq 0 ]; then
  step 'Checking ports'

  STACK_UP=0
  if stack_has_containers; then STACK_UP=1; fi

  CONFLICTS=''
  check_port() {
    local p="$1"; local name="$2"
    if port_free "$p"; then
      ok "port $p free ($name)"
    else
      local owner; owner="$(port_owner "$p")"
      if [ "$STACK_UP" -eq 1 ]; then
        warn "port $p in use by ${owner:-unknown} — most likely this Hermes stack already running."
      else
        bad "port $p in use by ${owner:-unknown} ($name)"
        CONFLICTS="$CONFLICTS $p"
      fi
    fi
  }

  check_port "$PORT_DASH"   'hermes-dashboard'
  check_port "$PORT_LLM"    'freellmapi'
  check_port "$PORT_API"    'hermes-core'
  check_port "$PORT_VIEWER" 'linkedin login viewer (noVNC)'

  if [ -n "$(printf '%s' "$CONFLICTS" | tr -d ' ')" ] && [ "$FORCE" -eq 0 ]; then
    printf '\n'
    info 'Fix options:'
    info '  a) stop whatever owns the port, or'
    info '  b) change the port in .env (HERMES_DASHBOARD_PORT / FREELLMAPI_PORT /'
    info '     HERMES_API_PORT / LINKEDIN_VIEWER_PORT) and re-run, or'
    info '  c) re-run with --force to continue anyway.'
    die "Ports already in use:$CONFLICTS"
  fi
fi

# ---------------------------------------------------------------------------
# 3. BUILD
# ---------------------------------------------------------------------------

if [ "$DO_PULL" -eq 1 ]; then
  step 'Pulling upstream images (freellmapi, linkedin-mcp)'
  if ! docker compose pull --ignore-buildable; then
    warn 'compose pull reported an error; continuing (build/up will surface anything fatal).'
  fi
fi

if [ "$SKIP_BUILD" -eq 0 ]; then
  BUILD_FLAGS=''
  if [ "$NO_CACHE" -eq 1 ]; then BUILD_FLAGS='--no-cache'; fi

  info 'First build downloads base images and installs dependencies. Expect 5-15 minutes.'
  step 'Building hermes-core and hermes-dashboard'
  # shellcheck disable=SC2086
  docker compose build $BUILD_FLAGS

  # The sandbox image is profile-gated so it never runs as a service, but
  # hermes-core needs it present locally to spawn ephemeral containers.
  step 'Building the sandbox image (profile build-only)'
  # shellcheck disable=SC2086
  docker compose --profile build-only build $BUILD_FLAGS
else
  warn 'Build skipped (--skip-build).'
fi

# ---------------------------------------------------------------------------
# 4. UP
# ---------------------------------------------------------------------------

CORE_DEFERRED=0
if [ -z "$FREE_KEY" ] && [ "$START_ALL" -eq 0 ]; then
  CORE_DEFERRED=1
  step 'Starting freellmapi, linkedin-mcp and hermes-dashboard'
  info 'hermes-core is held back: FREELLMAPI_KEY is still blank, so it has no LLM to talk to.'
  docker compose up -d freellmapi linkedin-mcp hermes-dashboard
else
  step 'Starting the stack'
  docker compose up -d
fi

step 'Container status'
docker compose ps || true

# ---------------------------------------------------------------------------
# 5. HEALTH PROBES (best effort — never fatal)
# ---------------------------------------------------------------------------

PROBE_HOST="$HOST_BIND_VAL"
if [ "$PROBE_HOST" = "0.0.0.0" ] || [ -z "$PROBE_HOST" ]; then PROBE_HOST='127.0.0.1'; fi

LLM_ROOT="http://${PROBE_HOST}:${PORT_LLM}"
DASH_ROOT="http://${PROBE_HOST}:${PORT_DASH}"
API_ROOT="http://${PROBE_HOST}:${PORT_API}"
VIEWER_ROOT="http://${PROBE_HOST}:${PORT_VIEWER}/vnc.html"

wait_http_ok "${LLM_ROOT}/api/ping" 90 "freellmapi (${LLM_ROOT}/api/ping)" || true
wait_http_ok "${DASH_ROOT}/" 60 "dashboard (${DASH_ROOT})" || true
if [ "$CORE_DEFERRED" -eq 0 ]; then
  wait_http_ok "${API_ROOT}/api/health" 90 "hermes-core (${API_ROOT}/api/health)" || true
fi

# ---------------------------------------------------------------------------
# 6. HANDOVER
# ---------------------------------------------------------------------------

head_ 'WHAT YOU MUST DO BY HAND'
cat <<EOF

These steps cannot be automated: they need a human account, a human password,
and a human clicking "I accept". Do them in this order.

  (1) MINT THE LLM ROUTER KEY
      a. Open the freellmapi dashboard:  ${LLM_ROOT}
      b. Create the first local account (you are the only user; this account
         lives inside your own container, not on anyone else's server).
         If you opened it from another device on your LAN, freellmapi prints a
         one-time setup code to its log instead of trusting the browser:
             docker compose logs freellmapi
      c. Add free provider keys. Each is free to create and independently
         rate-limited, so add several and let the router fail over:
             Google AI Studio, Groq, Cerebras, OpenRouter, Mistral, GitHub Models
      d. Create/copy the unified client key. It looks like:  freellmapi-xxxxxxxx
      e. Paste it into .env :
             FREELLMAPI_KEY=freellmapi-...
         (file: ${HERMES_ENV_FILE})
      f. Start (or restart) the orchestrator so it picks up the key:
             docker compose up -d hermes-core

  (2) LOG IN TO LINKEDIN (interactive, once per machine)
      Run:
             ./scripts/linkedin-login.sh
      Then open ${VIEWER_ROOT} and sign in by hand.
      You must complete 2FA / captcha yourself — Hermes does not and will not
      solve those. The authenticated browser profile persists in the
      \`linkedin-session\` Docker volume, so this survives restarts.

  (3) OPEN HERMES
             ${DASH_ROOT}
      Overview -> confirm LLM + LinkedIn + Docker are all green, then:
      LinkedIn page -> Import profile; Resume page -> Generate; Jobs page -> Search.

  REMINDER: Hermes never submits an application for you.
  It ranks jobs, tailors a resume, and hands you an apply link. You click it.

EOF

if [ "$CORE_DEFERRED" -eq 1 ]; then
  warn 'hermes-core is NOT running yet. Finish step (1) then: docker compose up -d hermes-core'
fi

info 'Docs: README.md  |  docs/ARCHITECTURE.md  |  docs/RUNBOOK.md'
printf '\n'
