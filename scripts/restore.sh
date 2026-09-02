#!/usr/bin/env bash
# Hermes — restore the Docker named volumes that hold your data, from a backup
# produced by scripts/backup.sh (or scripts\backup.ps1).
#
# Usage:
#   ./scripts/restore.sh [--from DIR] [--project NAME] [--helper-image IMG]
#                        [--down] [--force] [--dry-run]
#
#   --from DIR          Backup DIRECTORY to restore from. Default: the newest
#                       ./dist/backups/hermes-backup-* directory.
#   --project NAME      Compose project name override.
#   --helper-image IMG  Image used to run tar (default alpine:3.20).
#   --down              Bring the stack down instead of refusing when containers
#                       are running. Volumes are kept (`down` without -v).
#   --force             Do not prompt before emptying a non-empty volume. Does
#                       NOT make this script restore linkedin-session.
#   --dry-run           Validate everything and print the plan; write nothing.
#
# WHAT IS RESTORED
#   <backup dir>/hermes-data.tar.gz      -> <project>_hermes-data
#       SQLite DB (profile, resumes, jobs, runs, events, sandbox rows), rendered
#       .docx/.pdf/.txt files, per-run sandbox workspaces.
#   <backup dir>/freellmapi-data.tar.gz  -> <project>_freellmapi-data
#       The LLM router's local account and your ENCRYPTED provider keys.
#
# ---------------------------------------------------------------------------
# THIS IS DESTRUCTIVE. Each restored volume is EMPTIED first.
# ---------------------------------------------------------------------------
# There is no merge and no undo. You are asked to confirm per volume when the
# target is not empty; --force skips those prompts.
#
# ---------------------------------------------------------------------------
# linkedin-session IS NEVER RESTORED. There is no flag to override this.
# ---------------------------------------------------------------------------
# backup.sh refuses to copy it and this script refuses to write it, even if you
# hand it a linkedin-session.tar.gz from elsewhere. That volume is a live,
# authenticated LinkedIn browser profile: session cookies, auth tokens, device
# fingerprint. Moving it between machines is
#   (a) FRAGILE  — the fingerprint no longer matches the machine replaying it, so
#                  LinkedIn is more likely to invalidate the session or flag the
#                  account, and
#   (b) A SECURITY RISK — those files are bearer credentials to your LinkedIn
#                  account, in plain form, in every copy of the backup.
# Log in on the target machine instead:  ./scripts/linkedin-login.sh  (a minute,
# once). --force does not change this.
#
# ---------------------------------------------------------------------------
# ENCRYPTION_KEY
# ---------------------------------------------------------------------------
# freellmapi encrypted the provider keys inside freellmapi-data with the
# ENCRYPTION_KEY from the SOURCE machine's .env, and .env is not part of a
# backup. If this machine's ENCRYPTION_KEY differs, those keys are undecryptable
# and you must re-add them at http://localhost:3001.
#
# ---------------------------------------------------------------------------
# THE STACK MUST BE DOWN
# ---------------------------------------------------------------------------
# Replacing a volume under a running container leaves it holding deleted inodes,
# and for SQLite it is a good way to produce a corrupt database. This script
# refuses while any container of this compose project is running, or while any
# container is using a target volume. Run `make down` first, or pass --down.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
. "$SCRIPT_DIR/lib/common.sh"

FROM_DIR=''
PROJECT_OVERRIDE=''
HELPER_IMAGE='alpine:3.20'
BRING_DOWN=0
FORCE=0
DRY_RUN=0

# Logical volume names as declared in docker-compose.yml, in restore order.
# linkedin-session is absent on purpose — see the header.
RESTORE_VOLUMES='hermes-data freellmapi-data'
REFUSED_VOLUME='linkedin-session'

# Entry names that prove a hermes-data.tar.gz really came from Hermes. The
# archive is created with `tar -czf - -C /v .`, so paths look like `./hermes.db`.
HERMES_DATA_MARKERS='hermes.db resumes uploads workspaces renders'

while [ $# -gt 0 ]; do
  case "$1" in
    --from)         FROM_DIR="${2:-}"; shift ;;
    --project)      PROJECT_OVERRIDE="${2:-}"; shift ;;
    --helper-image) HELPER_IMAGE="${2:-}"; shift ;;
    --down)         BRING_DOWN=1 ;;
    --force)        FORCE=1 ;;
    --dry-run)      DRY_RUN=1 ;;
    -h|--help)      sed -n '2,62p' "$0"; exit 0 ;;
    *) die "Unknown option: $1 (try --help)" ;;
  esac
  shift
done

# ---------------------------------------------------------------------------
# The REAL compose project name.
#
# Same helper as backup.sh / load.sh: docker-compose.yml pins a `name:` key and,
# per the Compose Specification, that beats the directory basename which
# compose_project_name() falls back to. Precedence:
#     --project > COMPOSE_PROJECT_NAME (env) > COMPOSE_PROJECT_NAME (.env)
#     > `name:` in docker-compose.yml > directory basename
# Named volumes are prefixed with the winner, so getting this wrong means
# writing into the wrong project's volumes.
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

find_latest_backup() {
  # Newest ./dist/backups/hermes-backup-* directory, or empty.
  local root="$HERMES_ROOT/dist/backups"
  [ -d "$root" ] || return 0
  # The stamp is yyyymmdd-HHMMSS, so a reverse lexicographic sort is chronological.
  find "$root" -mindepth 1 -maxdepth 1 -type d -name 'hermes-backup-*' 2>/dev/null \
    | LC_ALL=C sort -r | head -n1
}

volume_exists() {
  docker volume inspect "$1" >/dev/null 2>&1
}

containers_using_volume() {
  # Ids of RUNNING containers with $1 mounted.
  docker ps -q --filter "volume=$1" 2>/dev/null | awk 'NF'
}

volume_entry_count() {
  # Top-level entries (dotfiles included) in a volume; empty string on error.
  local vol="$1"
  docker run --rm -v "${vol}:/v:ro" "$HELPER" sh -c 'ls -A /v | wc -l' 2>/dev/null | tr -d ' \r'
}

head_ 'HERMES RESTORE — data volumes'
cd "$HERMES_ROOT"
assert_docker_ready

PROJECT="$(hermes_project)"
ok "Compose project name: $PROJECT"

# ---------------------------------------------------------------------------
# 1. Locate and validate the backup directory. Nothing is touched until every
#    archive we intend to restore has been proved readable.
# ---------------------------------------------------------------------------
if [ -z "$FROM_DIR" ]; then
  step 'Looking for the newest backup under dist/backups'
  FROM_DIR="$(find_latest_backup || true)"
  if [ -z "$FROM_DIR" ]; then
    die "No backup directory given and none found under $HERMES_ROOT/dist/backups. Pass --from with the directory scripts/backup.sh wrote (named hermes-backup-<timestamp>, containing hermes-data.tar.gz)."
  fi
  ok "Using $FROM_DIR"
fi

if [ ! -e "$FROM_DIR" ]; then
  die "Backup path does not exist: $FROM_DIR"
fi
if [ ! -d "$FROM_DIR" ]; then
  die "--from must be the backup DIRECTORY, not a single file. You gave: $FROM_DIR. A Hermes backup is a folder containing hermes-data.tar.gz, freellmapi-data.tar.gz and MANIFEST.txt — pass the folder."
fi
FROM_DIR="$(cd "$FROM_DIR" && pwd)"
ok "Backup directory: $FROM_DIR"

step 'Validating the backup directory'
if [ -f "$FROM_DIR/MANIFEST.txt" ]; then
  if grep -q 'HERMES VOLUME BACKUP' "$FROM_DIR/MANIFEST.txt"; then
    ok 'MANIFEST.txt present and recognised'
    grep -E '^(created|source host|project name|stack quiesced)[[:space:]]*:' \
      "$FROM_DIR/MANIFEST.txt" 2>/dev/null | while IFS= read -r l; do info "$l"; done
  else
    warn 'MANIFEST.txt is present but does not look like a Hermes manifest.'
  fi
else
  warn 'No MANIFEST.txt in this directory. Continuing on the archive names alone.'
fi

PRESENT_LOGICAL=()
PRESENT_FILE=()
PRESENT_BYTES=()
for logical in $RESTORE_VOLUMES; do
  file="${logical}.tar.gz"
  if [ -f "$FROM_DIR/$file" ]; then
    bytes="$(wc -c < "$FROM_DIR/$file" | tr -d ' ')"
    ok "$file  $(human_bytes "$bytes")"
    PRESENT_LOGICAL+=("$logical")
    PRESENT_FILE+=("$file")
    PRESENT_BYTES+=("$bytes")
  else
    warn "$file is not in this backup — $logical will be left exactly as it is."
  fi
done
if [ "${#PRESENT_LOGICAL[@]}" -eq 0 ]; then
  die "This does not look like a Hermes backup: $FROM_DIR contains neither hermes-data.tar.gz nor freellmapi-data.tar.gz. Check the path, or re-create the backup with scripts/backup.sh."
fi

# Refuse the session volume loudly, wherever the archive came from.
if [ -f "$FROM_DIR/${REFUSED_VOLUME}.tar.gz" ]; then
  printf '\n'
  warn "${REFUSED_VOLUME}.tar.gz is in this backup and will NOT be restored."
  info 'That volume is a live authenticated LinkedIn browser profile: session'
  info 'cookies, auth tokens and a device fingerprint. Replaying it on another'
  info 'machine is fragile (the fingerprint no longer matches, so LinkedIn is'
  info 'more likely to invalidate the session or flag the account) AND a security'
  info 'risk (those files are bearer credentials to your LinkedIn account, in'
  info 'plain form). There is no flag that overrides this, including --force.'
  info ''
  info 'Log in on THIS machine instead, once:  ./scripts/linkedin-login.sh'
  printf '\n'
fi

# ---------------------------------------------------------------------------
# 2. Refuse to restore over a running stack.
# ---------------------------------------------------------------------------
step 'Checking that nothing is running'
RUNNING_COUNT=0
if RUNNING_IDS="$(docker compose ps -q 2>/dev/null)"; then
  RUNNING_COUNT="$(printf '%s\n' "$RUNNING_IDS" | awk 'NF' | wc -l | tr -d ' ')"
fi

if [ "$RUNNING_COUNT" -gt 0 ]; then
  if [ "$BRING_DOWN" -eq 1 ]; then
    warn "$RUNNING_COUNT container(s) running; bringing the stack down (--down)."
    docker compose --profile build-only --profile login down --remove-orphans
    ok 'Stack down (volumes kept)'
  else
    printf '\n'
    bad "$RUNNING_COUNT Hermes container(s) are still running. Refusing to restore."
    printf '\n'
    info 'Replacing a volume under a running container leaves that container holding'
    info 'deleted inodes, and for the SQLite database it is a good way to end up with'
    info 'a corrupt file. Bring the stack down first:'
    info ''
    info '    make down                 (or: docker compose down)'
    info ''
    info 'Then re-run this script. Or pass --down to have it do that for you.'
    printf '\n'
    exit 1
  fi
else
  ok 'No running containers in this compose project'
fi

# ---------------------------------------------------------------------------
# 3. Resolve the target volumes.
#
#    EXACT NAMES ONLY. common.sh's resolve_volume() has a last-resort suffix
#    match ("any volume ending in _hermes-data"), which is fine for a read-only
#    backup but dangerous here: on a machine that also has the separate
#    hermes-agent project it can resolve to `hermes_hermes-data`, and we would
#    wipe an unrelated project's data. So we only ever write <project>_<logical>.
# ---------------------------------------------------------------------------
step 'Resolving target volumes'
PLAN_LOGICAL=()
PLAN_FILE=()
PLAN_BYTES=()
PLAN_REAL=()
PLAN_EXISTS=()
i=0
while [ "$i" -lt "${#PRESENT_LOGICAL[@]}" ]; do
  logical="${PRESENT_LOGICAL[$i]}"
  real="${PROJECT}_${logical}"
  if volume_exists "$real"; then
    ok "$logical  ->  $real (exists)"
    exists=1
  else
    warn "$logical  ->  $real (does not exist yet; it will be created)"
    exists=0
  fi

  busy="$(containers_using_volume "$real" || true)"
  if [ -n "$busy" ]; then
    n="$(printf '%s\n' "$busy" | wc -l | tr -d ' ')"
    die "Volume $real is in use by $n running container(s) outside this compose project. Stop them first: docker ps --filter volume=$real"
  fi

  PLAN_LOGICAL+=("$logical")
  PLAN_FILE+=("${PRESENT_FILE[$i]}")
  PLAN_BYTES+=("${PRESENT_BYTES[$i]}")
  PLAN_REAL+=("$real")
  PLAN_EXISTS+=("$exists")
  i=$((i + 1))
done

# ---------------------------------------------------------------------------
# 4. Prove every archive is readable and really is what it claims to be, BEFORE
#    emptying anything.
#
#    The listing is done on the HOST with gzip+tar (the same way backup.sh
#    verifies what it wrote) rather than in a container, so it works even before
#    the helper image is resolved. The extract itself must be containerised —
#    the volume is only reachable from inside one.
# ---------------------------------------------------------------------------
HELPER="$(resolve_helper_image "$HELPER_IMAGE")"

step 'Verifying the archives'
PLAN_ENTRIES=()
i=0
while [ "$i" -lt "${#PLAN_LOGICAL[@]}" ]; do
  logical="${PLAN_LOGICAL[$i]}"
  file="${PLAN_FILE[$i]}"
  real="${PLAN_REAL[$i]}"
  path="$FROM_DIR/$file"

  entries=''
  if ! entries="$(gzip -dc "$path" 2>/dev/null | tar -tf - 2>/dev/null | wc -l | tr -d ' ')"; then
    entries=''
  fi
  if [ -z "$entries" ] || [ "$entries" = '0' ]; then
    # Fall back to the helper container: a host without gzip/tar (rare, but Git
    # Bash installs vary) must not be mistaken for a corrupt archive.
    entries="$(docker run --rm -i "$HELPER" sh -c 'tar -tzf - | wc -l' < "$path" 2>/dev/null | tr -d ' \r' || true)"
  fi
  if [ -z "$entries" ]; then
    die "$file is not a readable gzip tar. It is truncated, corrupt, or not a Hermes backup archive. Verify by hand with: gzip -dc '$path' | tar -tf - | head"
  fi
  if [ "$entries" -eq 0 ] 2>/dev/null; then
    die "$file contains zero entries. Restoring it would only empty $real. Refusing."
  fi

  if [ "$logical" = 'hermes-data' ]; then
    # './hermes.db' -> 'hermes.db' ; './resumes/x.docx' -> 'resumes'
    tops="$(gzip -dc "$path" 2>/dev/null | tar -tf - 2>/dev/null | head -n 400 \
            | sed -E 's#^\./##; s#/.*$##' | awk 'NF' | sort -u || true)"
    looks_right=0
    for marker in $HERMES_DATA_MARKERS; do
      if printf '%s\n' "$tops" | grep -qx "$marker"; then looks_right=1; break; fi
    done
    if [ "$looks_right" -eq 0 ]; then
      if [ "$FORCE" -eq 1 ]; then
        warn "$file has none of ($HERMES_DATA_MARKERS) at its top level. Continuing anyway (--force)."
      else
        die "$file does not look like a Hermes hermes-data archive: none of ($HERMES_DATA_MARKERS) appear at its top level. Refusing to overwrite $real with it. Pass --force if you are certain."
      fi
    fi
  fi

  ok "$file: $entries entries, readable"
  PLAN_ENTRIES+=("$entries")
  i=$((i + 1))
done

# ---------------------------------------------------------------------------
# 5. The plan, then consent.
# ---------------------------------------------------------------------------
printf '\n  RESTORE PLAN\n'
i=0
while [ "$i" -lt "${#PLAN_LOGICAL[@]}" ]; do
  printf '    %-24s -> %-34s (%s, %s entries)\n' \
    "${PLAN_FILE[$i]}" "${PLAN_REAL[$i]}" \
    "$(human_bytes "${PLAN_BYTES[$i]}")" "${PLAN_ENTRIES[$i]}"
  i=$((i + 1))
done
printf '    %-24s -> NOT RESTORED, deliberately\n\n' "$REFUSED_VOLUME"

if [ "$DRY_RUN" -eq 1 ]; then
  head_ 'DRY RUN — nothing was written'
  printf '  Re-run without --dry-run to apply this plan.\n\n'
  exit 0
fi

PLAN_SKIP=()
i=0
while [ "$i" -lt "${#PLAN_LOGICAL[@]}" ]; do
  logical="${PLAN_LOGICAL[$i]}"
  real="${PLAN_REAL[$i]}"
  file="${PLAN_FILE[$i]}"
  skip=0

  if [ "${PLAN_EXISTS[$i]}" -eq 0 ]; then
    # Create it with the labels Compose stamps on its own volumes, so a later
    # `docker compose up` adopts it instead of complaining that a volume of that
    # name exists but was not created by Compose.
    step "Creating volume $real"
    docker volume create \
      --label "com.docker.compose.project=${PROJECT}" \
      --label "com.docker.compose.volume=${logical}" \
      "$real" >/dev/null
    ok "$real created (empty)"
  else
    count="$(volume_entry_count "$real" || true)"
    if [ -z "$count" ]; then
      warn "$real: could not read its current contents; continuing."
    elif [ "$count" -gt 0 ] 2>/dev/null; then
      warn "$real is NOT empty ($count top-level entries). Restoring EMPTIES it first."
      if [ "$logical" = 'hermes-data' ]; then
        info 'That is your SQLite database, your generated resumes and your sandbox'
        info 'workspaces. There is no merge and no undo.'
      fi
      if [ "$logical" = 'freellmapi-data' ]; then
        info 'That is the LLM router account and its encrypted provider keys.'
      fi
      if [ "$FORCE" -eq 0 ]; then
        if ask_yes_no "Empty $real and replace it with $file?" no; then
          skip=0
        else
          warn "Skipping $logical — left untouched."
          skip=1
        fi
      fi
    else
      ok "$real is empty; nothing to overwrite"
    fi
  fi

  PLAN_SKIP+=("$skip")
  i=$((i + 1))
done

TODO=0
for s in "${PLAN_SKIP[@]}"; do
  if [ "$s" -eq 0 ]; then TODO=$((TODO + 1)); fi
done
if [ "$TODO" -eq 0 ]; then
  head_ 'NOTHING RESTORED'
  printf '  Every volume was skipped. No changes were made.\n\n'
  exit 0
fi

# ---------------------------------------------------------------------------
# 6. Restore.
#
#    The archive is STREAMED to the container on stdin rather than reached
#    through a bind-mounted host directory. Reason: Git Bash on Windows rewrites
#    POSIX-looking paths before Docker ever sees them, so `-v "$FROM_DIR:/backup"`
#    resolves to something inside the MSYS root. Redirection has no such problem.
#    (scripts/restore.ps1 does the opposite, for the opposite reason: PowerShell
#    5.1 corrupts binary data when it pipes native I/O.)
#
#    `find -mindepth 1 -maxdepth 1 -exec rm -rf {} +` empties the volume,
#    dotfiles included, without deleting the mount point itself.
# ---------------------------------------------------------------------------
RESTORED_LOGICAL=()
RESTORED_REAL=()
i=0
while [ "$i" -lt "${#PLAN_LOGICAL[@]}" ]; do
  if [ "${PLAN_SKIP[$i]}" -ne 0 ]; then i=$((i + 1)); continue; fi

  logical="${PLAN_LOGICAL[$i]}"
  real="${PLAN_REAL[$i]}"
  file="${PLAN_FILE[$i]}"
  path="$FROM_DIR/$file"

  step "Restoring $file  ->  $real"
  if ! after="$(docker run --rm -i -v "${real}:/v" "$HELPER" sh -c \
      'set -e; find /v -mindepth 1 -maxdepth 1 -exec rm -rf {} + ; tar -xzf - -C /v ; ls -A /v | wc -l' \
      < "$path" | tail -n1 | tr -d ' \r')"; then
    die "Restore of $logical FAILED. $real may now be partially written — re-run this script to try again, or \`docker volume rm $real\` and let \`make up\` recreate it empty. Reproduce by hand with: docker run --rm -i -v ${real}:/v $HELPER tar -xzf - -C /v < '$path'"
  fi
  ok "$logical restored (${after:-unknown} top-level entries in the volume)"
  RESTORED_LOGICAL+=("$logical")
  RESTORED_REAL+=("$real")
  i=$((i + 1))
done

# ---------------------------------------------------------------------------
# 7. Post-restore sanity: is the SQLite file actually there?
# ---------------------------------------------------------------------------
for idx in "${!RESTORED_LOGICAL[@]}"; do
  if [ "${RESTORED_LOGICAL[$idx]}" != 'hermes-data' ]; then continue; fi
  step 'Checking the restored database file'
  vol="${RESTORED_REAL[$idx]}"
  verdict="$(docker run --rm -v "${vol}:/v:ro" "$HELPER" sh -c \
    'if [ -f /v/hermes.db ]; then wc -c < /v/hermes.db; else echo MISSING; fi' 2>/dev/null | tr -d ' \r' || true)"
  if [ "$verdict" = 'MISSING' ]; then
    warn 'No /hermes.db in the restored volume. Hermes will create an EMPTY database on next start.'
    info 'That is expected only if the backup was taken before the stack ever ran.'
  elif [ -z "$verdict" ]; then
    warn 'Could not inspect the restored volume; check it by hand after `make up`.'
  else
    ok "hermes.db present, $(human_bytes "$verdict")"
  fi
done

# ---------------------------------------------------------------------------
# 8. ENCRYPTION_KEY cross-check for the router volume.
# ---------------------------------------------------------------------------
for idx in "${!RESTORED_LOGICAL[@]}"; do
  if [ "${RESTORED_LOGICAL[$idx]}" != 'freellmapi-data' ]; then continue; fi
  KEY="$(dotenv_get ENCRYPTION_KEY '')"
  printf '\n'
  if [ -z "$KEY" ]; then
    warn "ENCRYPTION_KEY is not set in this machine's .env."
    info 'freellmapi cannot even start without it, and the provider keys you just'
    info 'restored were encrypted with the value from the SOURCE machine. Put that'
    info 'exact value in .env before `make up`, or run `make bootstrap` to generate'
    info 'a fresh one and re-add your provider keys at http://localhost:3001.'
  else
    warn 'ENCRYPTION_KEY must MATCH the source machine, or the restored provider keys are unreadable.'
    info "This machine's .env has a key ending in ...$(printf '%s' "$KEY" | tail -c 6)"
    info 'If that is not the same value the backup was taken under, the router will'
    info 'start but every stored provider key will fail to decrypt — re-add them at'
    info 'http://localhost:3001 and mint a fresh FREELLMAPI_KEY.'
  fi
done

# ---------------------------------------------------------------------------
# Summary
# ---------------------------------------------------------------------------
head_ 'RESTORE COMPLETE'
printf '  From     : %s\n' "$FROM_DIR"
printf '  Project  : %s\n' "$PROJECT"
printf '  Volumes  : %s\n\n' "$(printf '%s ' "${RESTORED_REAL[@]}")"
printf '  Next:\n'
printf '    1) check ENCRYPTION_KEY in .env matches the source machine\n'
printf '    2) make up            (or: docker compose up -d)\n'
printf '    3) make health        (expect ok:true; llm/mcp may need setup)\n\n'
info "$REFUSED_VOLUME was NOT restored, deliberately. Log in on THIS machine:"
info '    ./scripts/linkedin-login.sh'
printf '\n'
