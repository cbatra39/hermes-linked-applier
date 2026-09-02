#!/usr/bin/env bash
# Hermes — archive the Docker named volumes that hold your data.
#
# Usage:
#   ./scripts/backup.sh [--out DIR] [--project NAME] [--helper-image IMG]
#                       [--live] [--no-restart] [--force]
#
#   --out DIR           Destination. Default: ./dist/backups/hermes-backup-<stamp>
#   --project NAME      Compose project name override (default: `hermes`).
#   --helper-image IMG  Image used to run tar (default alpine:3.20).
#   --live              Do not stop the stack first. Faster, but the SQLite DB
#                       may be captured mid-write. Not recommended.
#   --no-restart        If the stack was stopped for the snapshot, leave it down.
#   --force             Do not prompt; accept the recommended answers.
#
# WHAT IS BACKED UP
#   <project>_hermes-data      -> hermes-data.tar.gz
#       SQLite DB (profile, resumes, jobs, runs, events, sandbox rows), rendered
#       .docx/.pdf/.txt/.md files, per-run sandbox workspaces.
#   <project>_freellmapi-data  -> freellmapi-data.tar.gz
#       The LLM router's local account and your ENCRYPTED provider keys.
#
# ---------------------------------------------------------------------------
# linkedin-session IS DELIBERATELY NOT BACKED UP. This is not an oversight.
# ---------------------------------------------------------------------------
# That volume is a live, authenticated LinkedIn browser profile: session
# cookies, auth tokens, device fingerprint. Copying it is
#   (a) FRAGILE  — the fingerprint no longer matches the machine replaying it, so
#                  LinkedIn is more likely to invalidate the session or flag the
#                  account, and
#   (b) A SECURITY RISK — those files are bearer credentials to your LinkedIn
#                  account, in plain form, in every copy of the backup you keep.
# The supported way to have a session on a machine is to log in on that machine:
#   ./scripts/linkedin-login.sh   (about a minute, once). --force does not
# override this; there is no flag that copies it.
#
# ---------------------------------------------------------------------------
# ENCRYPTION_KEY
# ---------------------------------------------------------------------------
# freellmapi encrypts the provider keys inside freellmapi-data with the
# ENCRYPTION_KEY from .env. .env is NOT in this backup (it is a secret, and
# backups get emailed around). Restoring freellmapi-data without the matching
# ENCRYPTION_KEY leaves undecryptable keys. Record it in a password manager.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
. "$SCRIPT_DIR/lib/common.sh"

OUT_DIR=''
PROJECT_OVERRIDE=''
HELPER_IMAGE='alpine:3.20'
LIVE=0
NO_RESTART=0
FORCE=0

# Logical volume names as declared in docker-compose.yml. linkedin-session is
# absent on purpose — see the header.
BACKUP_VOLUMES='hermes-data freellmapi-data'
REFUSED_VOLUME='linkedin-session'

while [ $# -gt 0 ]; do
  case "$1" in
    --out)          OUT_DIR="${2:-}"; shift ;;
    --project)      PROJECT_OVERRIDE="${2:-}"; shift ;;
    --helper-image) HELPER_IMAGE="${2:-}"; shift ;;
    --live)         LIVE=1 ;;
    --no-restart)   NO_RESTART=1 ;;
    --force)        FORCE=1 ;;
    -h|--help)      sed -n '2,45p' "$0"; exit 0 ;;
    *) die "Unknown option: $1 (try --help)" ;;
  esac
  shift
done

# ---------------------------------------------------------------------------
# The REAL compose project name. docker-compose.yml pins `name: hermes`, which
# beats the directory basename that compose_project_name() falls back to.
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

resolve_helper_image() {
  # An image with tar in it. alpine:3.20 by default, pulled if missing; on an
  # air-gapped machine fall back to a Hermes image that is already local (all of
  # them ship tar + gzip).
  local preferred="$1"
  if docker image inspect "$preferred" --format '{{.Id}}' >/dev/null 2>&1; then
    ok "Helper image: $preferred (already local)" >&2
    printf '%s\n' "$preferred"; return 0
  fi
  step "Pulling the helper image $preferred" >&2
  if docker pull "$preferred" >&2; then
    ok "Helper image: $preferred" >&2
    printf '%s\n' "$preferred"; return 0
  fi
  warn "Could not pull $preferred (offline?). Looking for a local image with tar in it." >&2
  local cand
  for cand in hermes-core:latest hermes-linkedin/sandbox:latest hermes-dashboard:latest alpine:latest; do
    if docker image inspect "$cand" --format '{{.Id}}' >/dev/null 2>&1; then
      ok "Helper image: $cand (fallback)" >&2
      printf '%s\n' "$cand"; return 0
    fi
  done
  die "No usable helper image. Either get network access so \`docker pull $preferred\` works, or pass --helper-image with a local image that contains tar (\`docker image ls\`)."
}

head_ 'HERMES BACKUP — data volumes'
cd "$HERMES_ROOT"
assert_docker_ready

PROJECT="$(hermes_project)"
ok "Compose project name: $PROJECT"

STAMP="$(date '+%Y%m%d-%H%M%S')"
if [ -z "$OUT_DIR" ]; then OUT_DIR="$HERMES_ROOT/dist/backups/hermes-backup-$STAMP"; fi
mkdir -p "$OUT_DIR"
OUT_DIR="$(cd "$OUT_DIR" && pwd)"
ok "Output directory: $OUT_DIR"

# ---------------------------------------------------------------------------
# Resolve the volumes. A missing volume is not fatal on a fresh machine.
# ---------------------------------------------------------------------------
step 'Resolving Docker volumes'
PLAN_LOGICAL=()
PLAN_REAL=()
for logical in $BACKUP_VOLUMES; do
  real="$(resolve_volume "$logical" || true)"
  if [ -n "$real" ]; then
    ok "$logical  ->  $real"
    PLAN_LOGICAL+=("$logical")
    PLAN_REAL+=("$real")
  else
    warn "$logical: no such volume (expected ${PROJECT}_${logical}). Nothing to back up for it — has the stack ever been started?"
  fi
done
if [ "${#PLAN_LOGICAL[@]}" -eq 0 ]; then
  die "Neither hermes-data nor freellmapi-data exists for project \"$PROJECT\". Start the stack once (docker compose up -d) before backing it up, or pass --project."
fi

SESSION_VOL="$(resolve_volume "$REFUSED_VOLUME" || true)"
if [ -n "$SESSION_VOL" ]; then
  warn "$REFUSED_VOLUME ($SESSION_VOL) exists and is being SKIPPED on purpose."
  info 'It is a live authenticated LinkedIn browser profile. Copying it between'
  info 'machines is fragile (device fingerprint mismatch -> session invalidated or'
  info 'account flagged) AND a security risk (the files are bearer credentials to'
  info 'your LinkedIn account). Re-run scripts/linkedin-login.sh on the target'
  info 'machine instead. There is no flag to override this.'
fi

# ---------------------------------------------------------------------------
# Quiesce the stack so SQLite is not captured mid-write.
# ---------------------------------------------------------------------------
STOPPED_BY_US=0
RUNNING_COUNT=0
if RUNNING_IDS="$(docker compose ps -q 2>/dev/null)"; then
  RUNNING_COUNT="$(printf '%s\n' "$RUNNING_IDS" | awk 'NF' | wc -l | tr -d ' ')"
fi

if [ "$RUNNING_COUNT" -gt 0 ]; then
  if [ "$LIVE" -eq 1 ]; then
    warn 'Backing up with the stack RUNNING (--live).'
    info 'hermes-core writes SQLite in WAL mode, so this snapshot can land between a'
    info 'commit and its WAL checkpoint. The restore usually works and occasionally'
    info 'loses the last few writes. Do not use --live for a migration you care about.'
  else
    step "Stopping $RUNNING_COUNT running container(s) for a consistent snapshot"
    DO_STOP=1
    if [ "$FORCE" -eq 0 ]; then
      if ask_yes_no 'Stop the Hermes containers while the snapshot is taken (recommended)?' yes; then
        DO_STOP=1
      else
        DO_STOP=0
      fi
    fi
    if [ "$DO_STOP" -eq 1 ]; then
      docker compose stop
      STOPPED_BY_US=1
      ok 'Stack stopped (containers kept, volumes untouched)'
    else
      warn 'Continuing with the stack running; see the --live caveat above.'
    fi
  fi
else
  ok 'No running containers — snapshot will be consistent.'
fi

restart_if_needed() {
  if [ "$STOPPED_BY_US" -eq 1 ]; then
    if [ "$NO_RESTART" -eq 1 ]; then
      warn 'Stack left stopped (--no-restart). Start it with: docker compose up -d'
    else
      step 'Restarting the stack'
      if docker compose start; then ok 'Stack restarted'; else warn '`docker compose start` failed; try: docker compose up -d'; fi
    fi
    STOPPED_BY_US=0
  fi
}
trap restart_if_needed EXIT

HELPER="$(resolve_helper_image "$HELPER_IMAGE")"

# ---------------------------------------------------------------------------
# Archive each volume.
#
# The tar is STREAMED to stdout and redirected by the shell, rather than written
# into a bind-mounted host directory. Reason: Git Bash on Windows rewrites
# POSIX-looking paths before Docker ever sees them, so `-v "$OUT_DIR:/backup"`
# resolves to something inside the MSYS root. Redirection has no such problem.
# (scripts/backup.ps1 does the opposite, for the opposite reason: PowerShell 5.1
# corrupts binary output when you redirect a native command.)
# ---------------------------------------------------------------------------
RESULT_LINES=()
i=0
while [ "$i" -lt "${#PLAN_LOGICAL[@]}" ]; do
  logical="${PLAN_LOGICAL[$i]}"
  real="${PLAN_REAL[$i]}"
  file="${logical}.tar.gz"
  target="$OUT_DIR/$file"

  step "Archiving $real  ->  $file"
  rm -f "$target"
  docker run --rm -v "${real}:/v:ro" "$HELPER" tar -czf - -C /v . > "$target"

  if [ ! -s "$target" ]; then
    die "tar produced an empty $target. Is the volume readable? Try: docker run --rm -v ${real}:/v:ro $HELPER ls -la /v"
  fi
  bytes="$(wc -c < "$target" | tr -d ' ')"
  if [ "$bytes" -lt 100 ]; then
    warn "$file is only ${bytes} bytes — the volume is probably empty."
  fi
  ok "$file  $(human_bytes "$bytes")"

  # Verify the archive is readable and count its entries.
  entries='unknown'
  if e="$(gzip -dc "$target" 2>/dev/null | tar -tf - 2>/dev/null | wc -l | tr -d ' ')"; then
    if [ -n "$e" ]; then entries="$e"; fi
  fi
  if [ "$entries" = 'unknown' ]; then
    warn "$file: could not verify the archive listing."
  else
    ok "$file: verified, $entries entries"
  fi

  RESULT_LINES+=("$(printf '  %-24s %-28s %12s  %s entries' "$file" "$real" "$(human_bytes "$bytes")" "$entries")")
  i=$((i + 1))
done

restart_if_needed
trap - EXIT

# ---------------------------------------------------------------------------
# MANIFEST.txt — a backup nobody can interpret is not a backup.
# ---------------------------------------------------------------------------
step 'Writing MANIFEST.txt'
if [ "$STOPPED_BY_US" -eq 1 ]; then QUIESCED='yes'; else QUIESCED='see below'; fi
QUIESCED_TEXT='yes (containers stopped for the snapshot)'
if [ "$LIVE" -eq 1 ] || [ "$RUNNING_COUNT" -gt 0 ] && [ "$QUIESCED" = 'see below' ]; then
  QUIESCED_TEXT='NO - taken live; the SQLite DB may be mid-write'
fi

{
  printf '=============================================================================\n'
  printf ' HERMES VOLUME BACKUP\n'
  printf '=============================================================================\n'
  printf 'created        : %s\n' "$(date '+%Y-%m-%d %H:%M:%S')"
  printf 'source host    : %s\n' "$(hostname 2>/dev/null || printf 'unknown')"
  printf 'project name   : %s\n' "$PROJECT"
  printf 'stack quiesced : %s\n' "$QUIESCED_TEXT"
  printf '\nCONTENTS\n'
  for line in "${RESULT_LINES[@]}"; do printf '%s\n' "$line"; done
  cat <<EOF

NOT INCLUDED, ON PURPOSE
  ${PROJECT}_${REFUSED_VOLUME}
      A live authenticated LinkedIn browser profile (cookies, tokens, device
      fingerprint). Never copy it between machines: the fingerprint mismatch
      makes LinkedIn more likely to invalidate the session or flag the account,
      and the files are bearer credentials to your account in plain form.
      On the target machine run:  scripts/linkedin-login.sh
  .env
      Holds ENCRYPTION_KEY and FREELLMAPI_KEY. Secrets do not travel in a
      backup archive.

YOU MUST ALSO RECORD, SEPARATELY:
      ENCRYPTION_KEY  (from .env on this machine)
      freellmapi encrypted the provider keys inside freellmapi-data with it.
      Restore that volume without the same ENCRYPTION_KEY and those keys are
      unreadable — you would have to re-add them in the router dashboard.
      Put it in a password manager, not in this folder.

RESTORE
      Linux/macOS :  ./scripts/restore.sh --from "$OUT_DIR"
      Windows     :  .\\scripts\\restore.ps1 -From <this directory>
      The stack must be down; restore brings it down for you.

TREAT THIS FOLDER LIKE A PASSWORD FILE. It contains your scraped LinkedIn
profile, your resumes, and your encrypted LLM provider keys.
=============================================================================
EOF
} > "$OUT_DIR/MANIFEST.TXT.tmp"
mv "$OUT_DIR/MANIFEST.TXT.tmp" "$OUT_DIR/MANIFEST.txt"
ok "Wrote $OUT_DIR/MANIFEST.txt"

# ---------------------------------------------------------------------------
# Summary
# ---------------------------------------------------------------------------
TOTAL_BYTES="$(du -sk "$OUT_DIR" 2>/dev/null | awk '{print $1*1024}')"
if [ -z "$TOTAL_BYTES" ]; then TOTAL_BYTES=0; fi

head_ 'BACKUP COMPLETE'
printf '  Location : %s\n' "$OUT_DIR"
printf '  Size     : %s\n' "$(human_bytes "$TOTAL_BYTES")"
printf '  Volumes  : %s\n\n' "$(printf '%s ' "${PLAN_LOGICAL[@]}")"
printf '  Restore with:\n'
printf '    ./scripts/restore.sh --from "%s"\n\n' "$OUT_DIR"
warn 'Record your ENCRYPTION_KEY from .env somewhere safe, or the restored'
warn 'freellmapi provider keys will be undecryptable.'
printf '\n'
info "$REFUSED_VOLUME was NOT copied, deliberately. Re-login on the target machine:"
info '    ./scripts/linkedin-login.sh'
printf '\n'
