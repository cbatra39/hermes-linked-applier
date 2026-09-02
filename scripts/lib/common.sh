#!/usr/bin/env bash
# Hermes — shared helpers for all shell scripts.
#
# Source from a script in scripts/ :
#     SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
#     . "$SCRIPT_DIR/lib/common.sh"
#
# Targets bash 4+ on Linux/macOS and Git Bash on Windows.

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

HERMES_LIB_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
HERMES_SCRIPTS_DIR="$(cd "$HERMES_LIB_DIR/.." && pwd)"
HERMES_ROOT="$(cd "$HERMES_SCRIPTS_DIR/.." && pwd)"
HERMES_ENV_FILE="$HERMES_ROOT/.env"
HERMES_ENV_EXAMPLE="$HERMES_ROOT/.env.example"

export HERMES_ROOT HERMES_SCRIPTS_DIR HERMES_ENV_FILE HERMES_ENV_EXAMPLE

# ---------------------------------------------------------------------------
# Console output
# ---------------------------------------------------------------------------

if [ -t 1 ]; then
  C_RESET=$'\033[0m'; C_CYAN=$'\033[36m'; C_GREEN=$'\033[32m'
  C_YELLOW=$'\033[33m'; C_RED=$'\033[31m'; C_GRAY=$'\033[90m'
else
  C_RESET=''; C_CYAN=''; C_GREEN=''; C_YELLOW=''; C_RED=''; C_GRAY=''
fi

head_()  { printf '\n%s\n  %s\n%s\n' "$C_CYAN==========================================================================$C_RESET" "$C_CYAN$*$C_RESET" "$C_CYAN==========================================================================$C_RESET"; }
step()   { printf '%s==> %s%s\n' "$C_CYAN" "$*" "$C_RESET"; }
ok()     { printf '%s  [ ok ] %s%s\n' "$C_GREEN" "$*" "$C_RESET"; }
warn()   { printf '%s  [warn] %s%s\n' "$C_YELLOW" "$*" "$C_RESET"; }
bad()    { printf '%s  [FAIL] %s%s\n' "$C_RED" "$*" "$C_RESET"; }
info()   { printf '%s         %s%s\n' "$C_GRAY" "$*" "$C_RESET"; }

die() { printf '\n'; bad "$*"; printf '\n'; exit 1; }

have() { command -v "$1" >/dev/null 2>&1; }

# ---------------------------------------------------------------------------
# Docker / compose discovery
# ---------------------------------------------------------------------------

assert_docker_ready() {
  step 'Checking Docker'

  have docker || die 'docker is not on PATH. Install Docker Engine or Docker Desktop, then reopen this shell.'

  local sv
  if ! sv="$(docker info --format '{{.ServerVersion}}' 2>/dev/null)"; then
    die 'Docker engine is not responding. Start the Docker daemon (or Docker Desktop) and re-run.'
  fi
  ok "Docker engine ${sv:-reachable}"

  local cv
  if ! cv="$(docker compose version --short 2>/dev/null)"; then
    die 'Docker Compose v2 is missing (`docker compose version` failed). Hermes needs the `docker compose` subcommand, not the legacy `docker-compose` binary.'
  fi
  ok "Docker Compose ${cv:-v2}"
}

compose_project_name() {
  if [ -n "${COMPOSE_PROJECT_NAME:-}" ]; then
    printf '%s\n' "$COMPOSE_PROJECT_NAME"; return 0
  fi
  local from_env
  from_env="$(dotenv_get COMPOSE_PROJECT_NAME '')"
  if [ -n "$from_env" ]; then
    printf '%s\n' "$from_env"; return 0
  fi
  local base
  base="$(basename "$HERMES_ROOT" | tr '[:upper:]' '[:lower:]' | tr -cd 'a-z0-9_-')"
  if [ -z "$base" ]; then base='hermes'; fi
  printf '%s\n' "$base"
}

compose_images() {
  # Every image this project uses, including the build-only and login profiles.
  local out
  if out="$(cd "$HERMES_ROOT" && docker compose --profile build-only --profile login config --images 2>/dev/null)" \
     && [ -n "$out" ]; then
    printf '%s\n' "$out" | awk 'NF' | awk '!seen[$0]++'
    return 0
  fi

  warn '`docker compose config --images` unavailable; predicting image names from the project name.'
  local proj; proj="$(compose_project_name)"
  cat <<EOF
ghcr.io/tashfeenahmed/freellmapi:latest
stickerdaniel/linkedin-mcp-server:4.23.2
${proj}-hermes-core
${proj}-hermes-dashboard
hermes-linkedin/sandbox:latest
EOF
}

stack_has_containers() {
  local ids
  ids="$(cd "$HERMES_ROOT" && docker compose ps -aq 2>/dev/null)" || return 1
  [ -n "$ids" ]
}

resolve_volume() {
  # resolve_volume <logical-name> -> real docker volume name, or empty.
  local logical="$1"
  local proj; proj="$(compose_project_name)"
  local c
  for c in "${proj}_${logical}" "$logical"; do
    if docker volume inspect "$c" >/dev/null 2>&1; then
      printf '%s\n' "$c"; return 0
    fi
  done
  docker volume ls --format '{{.Name}}' 2>/dev/null | grep -E "_${logical}\$" | head -n1
}

docker_path() {
  # Normalise a host path for `docker run -v`. On Git Bash, /c/x must become C:/x
  # or Docker Desktop resolves it inside the MSYS root.
  local p="$1"
  if [ -d "$p" ]; then p="$(cd "$p" && pwd)"; fi
  case "$(uname -s 2>/dev/null)" in
    MINGW*|MSYS*|CYGWIN*)
      # /c/Users/me -> C:/Users/me
      printf '%s\n' "$p" | sed -E 's#^/([a-zA-Z])/#\U\1:/#'
      ;;
    *) printf '%s\n' "$p" ;;
  esac
}

# ---------------------------------------------------------------------------
# .env handling
# ---------------------------------------------------------------------------

dotenv_get() {
  # dotenv_get KEY [DEFAULT] — reads $HERMES_ENV_FILE. Empty value => default.
  local key="$1"; local def="${2:-}"
  local val=''
  if [ -f "$HERMES_ENV_FILE" ]; then
    val="$(sed -n -E "s/^[[:space:]]*${key}[[:space:]]*=[[:space:]]*(.*)[[:space:]]*\$/\1/p" "$HERMES_ENV_FILE" | tail -n1)"
    # strip matching surrounding quotes
    val="$(printf '%s' "$val" | sed -E 's/^"(.*)"$/\1/; s/^'"'"'(.*)'"'"'$/\1/')"
  fi
  if [ -z "$val" ]; then printf '%s\n' "$def"; else printf '%s\n' "$val"; fi
}

dotenv_set() {
  # dotenv_set KEY VALUE — in-place update of $HERMES_ENV_FILE, preserving
  # comments and order. Appends when the key is absent.
  local key="$1"; local value="$2"
  local file="$HERMES_ENV_FILE"
  touch "$file"
  if grep -qE "^[[:space:]]*${key}[[:space:]]*=" "$file"; then
    local tmp; tmp="$(mktemp)"
    # Use awk (not sed) so the value can contain / & and other sed metacharacters.
    KEY="$key" VALUE="$value" awk '
      BEGIN { k = ENVIRON["KEY"]; v = ENVIRON["VALUE"]; done = 0 }
      {
        line = $0
        if (!done && line ~ ("^[ \t]*" k "[ \t]*=")) { print k "=" v; done = 1; next }
        print line
      }
    ' "$file" > "$tmp"
    mv "$tmp" "$file"
  else
    printf '%s=%s\n' "$key" "$value" >> "$file"
  fi
}

new_hex_key() {
  # 32 random bytes as 64 lowercase hex characters.
  local n="${1:-32}"
  if have openssl; then
    openssl rand -hex "$n"; return 0
  fi
  if have python3; then
    python3 -c "import secrets,sys; sys.stdout.write(secrets.token_hex(int(sys.argv[1])))" "$n"; printf '\n'; return 0
  fi
  if have xxd && [ -r /dev/urandom ]; then
    head -c "$n" /dev/urandom | xxd -p | tr -d '\n'; printf '\n'; return 0
  fi
  if have od && [ -r /dev/urandom ]; then
    head -c "$n" /dev/urandom | od -An -tx1 | tr -d ' \n'; printf '\n'; return 0
  fi
  die 'Cannot generate a random key: install openssl or python3.'
}

env_example_fallback() {
  cat <<'EOF'
# Hermes configuration. Generated by scripts/bootstrap because .env.example was missing.
HOST_BIND=127.0.0.1
ENCRYPTION_KEY=
FREELLMAPI_BASE_URL=http://freellmapi:3001/v1
FREELLMAPI_KEY=
HERMES_MODEL_PRIMARY=
HERMES_MODEL_FALLBACKS=
LINKEDIN_MCP_URL=http://linkedin-mcp:8000/mcp
HERMES_API_PORT=8080
HERMES_DASHBOARD_PORT=3000
FREELLMAPI_PORT=3001
LINKEDIN_VIEWER_PORT=6080
HERMES_SANDBOX_IMAGE=hermes-linkedin/sandbox:latest
HERMES_SANDBOX_MEMORY_MB=1024
HERMES_SANDBOX_CPUS=1.0
HERMES_SANDBOX_TIMEOUT_S=300
HERMES_SANDBOX_NETWORK=none
HERMES_SANDBOX_WORKSPACE=/data/workspaces
HERMES_DATA_DIR=/data
HERMES_DOCKER_HOST=unix:///var/run/docker.sock
LOG_LEVEL=INFO
EOF
}

# ---------------------------------------------------------------------------
# Ports
# ---------------------------------------------------------------------------

port_free() {
  # Return 0 when nothing is listening on TCP <port>.
  local port="$1"

  if have ss; then
    if ss -ltnH 2>/dev/null | awk '{print $4}' | grep -qE "[:.]${port}\$"; then return 1; fi
    return 0
  fi
  if have lsof; then
    if lsof -nP -iTCP:"$port" -sTCP:LISTEN >/dev/null 2>&1; then return 1; fi
    return 0
  fi
  if have netstat; then
    if netstat -an 2>/dev/null | grep -iE 'listen' | awk '{print $4}' | grep -qE "[:.]${port}\$"; then return 1; fi
    return 0
  fi
  # Last resort: try to connect. A successful connect means something is there.
  if (exec 3<>"/dev/tcp/127.0.0.1/${port}") >/dev/null 2>&1; then
    exec 3<&- 2>/dev/null || true
    return 1
  fi
  return 0
}

port_owner() {
  local port="$1"
  if have lsof; then
    lsof -nP -iTCP:"$port" -sTCP:LISTEN -Fcp 2>/dev/null | tr '\n' ' ' | sed 's/[cp]/ /g' | awk '{print $1" "$2}'
    return 0
  fi
  if have ss; then
    ss -ltnpH 2>/dev/null | awk -v p=":$port" '$4 ~ p {print $NF}' | head -n1
    return 0
  fi
  printf 'unknown process\n'
}

# ---------------------------------------------------------------------------
# HTTP probe
# ---------------------------------------------------------------------------

http_ok() {
  # 0 when something answers HTTP at <url> (any status).
  local url="$1"; local timeout="${2:-5}"
  if have curl; then
    curl -fsS -o /dev/null --max-time "$timeout" "$url" >/dev/null 2>&1 && return 0
    # Any HTTP status still proves the port answers.
    local code
    code="$(curl -s -o /dev/null -w '%{http_code}' --max-time "$timeout" "$url" 2>/dev/null)"
    [ -n "$code" ] && [ "$code" != "000" ] && return 0
    return 1
  fi
  if have wget; then
    wget -q -T "$timeout" -O /dev/null "$url" >/dev/null 2>&1 && return 0
    return 1
  fi
  return 1
}

wait_http_ok() {
  local url="$1"; local timeout="${2:-90}"; local label="${3:-$1}"
  step "Waiting for $label"
  local waited=0
  while [ "$waited" -lt "$timeout" ]; do
    if http_ok "$url" 4; then ok "$label is answering"; return 0; fi
    sleep 3
    waited=$((waited + 3))
  done
  warn "$label did not answer within ${timeout}s (it may still be starting)."
  return 1
}

ask_yes_no() {
  # ask_yes_no "Question" [default:yes|no] -> 0 for yes
  local q="$1"; local def="${2:-no}"
  local suffix=' [y/N] '
  if [ "$def" = "yes" ]; then suffix=' [Y/n] '; fi
  while true; do
    printf '%s%s' "$q" "$suffix"
    local a; read -r a || a=''
    a="$(printf '%s' "$a" | tr '[:upper:]' '[:lower:]' | tr -d '[:space:]')"
    if [ -z "$a" ]; then
      if [ "$def" = "yes" ]; then return 0; fi
      return 1
    fi
    case "$a" in
      y|yes) return 0 ;;
      n|no)  return 1 ;;
      *) printf 'Please answer y or n.\n' ;;
    esac
  done
}

human_bytes() {
  local b="$1"
  if [ "$b" -ge 1073741824 ] 2>/dev/null; then awk -v b="$b" 'BEGIN{printf "%.2f GB\n", b/1073741824}'; return; fi
  if [ "$b" -ge 1048576 ] 2>/dev/null;    then awk -v b="$b" 'BEGIN{printf "%.1f MB\n", b/1048576}'; return; fi
  if [ "$b" -ge 1024 ] 2>/dev/null;       then awk -v b="$b" 'BEGIN{printf "%.1f KB\n", b/1024}'; return; fi
  printf '%s B\n' "$b"
}
