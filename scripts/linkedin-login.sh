#!/usr/bin/env bash
# Hermes — interactive LinkedIn sign-in for the MCP scraper.
#
# Usage:
#   ./scripts/linkedin-login.sh [--keep-running] [--no-restart] [--yes]
#
#   --keep-running  Do not stop/restart linkedin-mcp around the login run.
#   --no-restart    Stop linkedin-mcp but do not bring it back up afterwards.
#   --yes           Do not ask for confirmation before starting the container.
#
# Starts the one-shot `linkedin-login` service (the MCP image with
# `--login --login-viewer`), which publishes a noVNC desktop on
# LINKEDIN_VIEWER_PORT. You open that URL and sign in to LinkedIn with your own
# hands: password, 2FA, captcha. Hermes does not automate sign-in and cannot
# solve captchas — that is deliberate.
#
# The authenticated browser profile lands in /home/pwuser/.linkedin-mcp inside
# the NAMED VOLUME `linkedin-session`, which the long-running linkedin-mcp
# service reads on its next start.
#
# Chromium takes an exclusive lock on the profile directory, so the login
# container and linkedin-mcp cannot both hold it. This script stops the service
# first and restarts it afterwards.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
. "$SCRIPT_DIR/lib/common.sh"

KEEP_RUNNING=0
NO_RESTART=0
ASSUME_YES=0

while [ $# -gt 0 ]; do
  case "$1" in
    --keep-running) KEEP_RUNNING=1 ;;
    --no-restart)   NO_RESTART=1 ;;
    --yes|-y)       ASSUME_YES=1 ;;
    -h|--help)      sed -n '2,28p' "$0"; exit 0 ;;
    *) die "Unknown option: $1 (try --help)" ;;
  esac
  shift
done

head_ 'HERMES — LINKEDIN INTERACTIVE LOGIN'
cd "$HERMES_ROOT"

assert_docker_ready

if [ ! -f "$HERMES_ENV_FILE" ]; then
  warn '.env not found — run ./scripts/bootstrap.sh first. Falling back to default ports.'
fi

PORT_VIEWER="$(dotenv_get LINKEDIN_VIEWER_PORT 6080)"
HOST_BIND_VAL="$(dotenv_get HOST_BIND 127.0.0.1)"
PROBE_HOST="$HOST_BIND_VAL"
if [ "$PROBE_HOST" = "0.0.0.0" ] || [ -z "$PROBE_HOST" ]; then PROBE_HOST='127.0.0.1'; fi
VIEWER_URL="http://${PROBE_HOST}:${PORT_VIEWER}/vnc.html"

# ---------------------------------------------------------------------------
# Release the browser-profile lock held by the running MCP service.
# ---------------------------------------------------------------------------
STOPPED=0
if [ "$KEEP_RUNNING" -eq 0 ]; then
  step 'Stopping linkedin-mcp so the login container can take the browser profile lock'
  if docker compose stop linkedin-mcp; then
    STOPPED=1
    ok 'linkedin-mcp stopped'
  else
    warn 'Could not stop linkedin-mcp (it may not be running). Continuing.'
  fi
else
  warn 'Leaving linkedin-mcp alone (--keep-running). If login fails with a profile lock error, stop it first.'
fi

if ! port_free "$PORT_VIEWER"; then
  warn "Port $PORT_VIEWER is already in use by $(port_owner "$PORT_VIEWER"). The viewer may fail to publish."
  info 'If this is a leftover login container: docker compose --profile login rm -f linkedin-login'
fi

# ---------------------------------------------------------------------------
# Instructions BEFORE the blocking run — the container output will scroll.
# ---------------------------------------------------------------------------
cat <<EOF

-------------------------------------------------------------------------
 READ THIS FIRST — then the container starts and takes over the terminal
-------------------------------------------------------------------------

  1. Wait for a log line saying the viewer / noVNC is listening.
  2. Open this URL in your browser:
       ${VIEWER_URL}
     (If it asks for a VNC password, just press Connect — none is set.)

  3. In the remote browser window, sign in to LinkedIn YOURSELF:
       - email + password
       - the 2FA / one-time code, if your account uses one
       - any captcha or "is this you?" challenge
     Hermes will not do this part for you and cannot solve captchas.

  4. Land on your LinkedIn feed. That is what "logged in" means here.
  5. The container detects the session, saves the browser profile, and exits.
     If it does not exit on its own, press Ctrl+C once you are on the feed.

  The session is stored in the Docker volume \`linkedin-session\`.
  It survives restarts and reboots. It does NOT travel between machines —
  see scripts/backup.sh and the README: re-login on the new laptop instead.

EOF

if [ "$ASSUME_YES" -eq 0 ]; then
  if ! ask_yes_no 'Start the login container now?' yes; then
    warn 'Aborted by user.'
    if [ "$STOPPED" -eq 1 ] && [ "$NO_RESTART" -eq 0 ]; then
      step 'Restarting linkedin-mcp'
      docker compose up -d linkedin-mcp || true
    fi
    exit 0
  fi
fi

head_ 'LOGIN CONTAINER OUTPUT'
printf 'Open: %s\n\n' "$VIEWER_URL"

# --service-ports is what actually publishes 6080 for a one-shot `run`
# container; without it the viewer is unreachable from the host.
LOGIN_RC=0
docker compose --profile login run --rm --service-ports linkedin-login || LOGIN_RC=$?

printf '\n'
if [ "$LOGIN_RC" -eq 0 ]; then
  ok 'Login container exited cleanly.'
else
  warn "Login container exited with code ${LOGIN_RC}. If you completed the sign-in before Ctrl+C, the session was probably still saved — verify below."
fi

# ---------------------------------------------------------------------------
# Bring the scraper back up and verify.
# ---------------------------------------------------------------------------
if [ "$STOPPED" -eq 1 ] && [ "$NO_RESTART" -eq 0 ]; then
  step 'Restarting linkedin-mcp'
  docker compose up -d linkedin-mcp
fi

PORT_API="$(dotenv_get HERMES_API_PORT 8080)"
PORT_DASH="$(dotenv_get HERMES_DASHBOARD_PORT 3000)"

head_ 'VERIFY'
cat <<EOF
Check the authenticated state from Hermes:

  API:       curl http://${PROBE_HOST}:${PORT_API}/api/linkedin/status
  Dashboard: http://${PROBE_HOST}:${PORT_DASH}  ->  LinkedIn page

Expected: {"reachable": true, "authenticated": true, ...}
If authenticated is false, re-run this script and make sure you reach the feed.

Note: LinkedIn sessions expire and LinkedIn may invalidate them after unusual
activity. Re-running this script is the normal fix.

EOF
