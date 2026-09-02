# Hermes — Runbook

Operational procedures for the Hermes stack. Every command below exists in the
`Makefile` or in `scripts/`. Architecture and rationale live in
[ARCHITECTURE.md](ARCHITECTURE.md).

**Two entry points, same work.** `make <target>` is a thin wrapper — nothing is
hidden. On Windows, `make` is not installed with Docker Desktop, so use the
PowerShell scripts (`scripts\*.ps1`) or run the `docker compose` line by hand.
Where a Makefile target and a script both exist, the scripts do more preflight
and more explaining.

**Project name is `hermes-linkedin`.** Volumes are
`hermes-linkedin_hermes-data`, `hermes-linkedin_freellmapi-data`,
`hermes-linkedin_linkedin-session`.

> **Port contention.** A separate `hermes-agent` project on this machine already
> publishes its own freellmapi on `3001`. Run one stack at a time, or change
> `FREELLMAPI_PORT` / `HERMES_DASHBOARD_PORT` in `.env`.

---

## 1. First run

Total wall-clock: **35–75 minutes**, almost all of it the sandbox image build
(LibreOffice) and — if you have no session yet — your own typing in the LinkedIn
viewer. Nothing here is destructive and every step is re-runnable.

### Step 0 — prerequisites

Docker Engine or Docker Desktop with **Compose v2** (`docker compose`, not the
legacy `docker-compose` binary). Every script checks this first and fails with a
specific remedy.

```bash
docker info --format '{{.ServerVersion}}'
docker compose version --short
```

Budget ~10 GB of disk: the sandbox image alone is ~728 MB built, the MCP image
carries a Playwright/Chromium runtime, and `make ship` writes a multi-GB tar.

### Step 1 — configuration (≈5 seconds)

```bash
make bootstrap
```
```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\bootstrap.ps1
```

`make bootstrap` copies `.env.example` → `.env` (leaving an existing `.env`
untouched) and generates `ENCRYPTION_KEY` — 64 hex characters, used by
`freellmapi` to encrypt its provider-key store. Compose refuses to start without
it: the `freellmapi` service declares
`ENCRYPTION_KEY: ${ENCRYPTION_KEY:?...}`.

`scripts/bootstrap.ps1` / `.sh` do more: Docker preflight, port availability
checks, `.env` creation and key generation, image pull, build, and it brings up
everything **except** `hermes-core` while `FREELLMAPI_KEY` is still blank. Flags:
`--skip-build`, `--skip-preflight`, `--force` (port conflicts become warnings),
`--all` (start `hermes-core` anyway), `--pull`, `--no-cache`
(PowerShell: `-SkipBuild`, `-SkipPreflight`, `-Force`, `-All`, `-Pull`,
`-NoCache`).

**Working looks like:** `.env` exists, `ENCRYPTION_KEY=` has 64 hex characters,
`FREELLMAPI_KEY=` is still empty. That is expected — it cannot be generated
offline.

### Step 2 — build (25–50 minutes cold)

```bash
make build
```

Pulls the two pinned third-party images and builds the three first-party ones.
`--profile build-only` is what makes the sandbox image build at all.

| Image | Cold build | Notes |
|---|---|---|
| `ghcr.io/tashfeenahmed/freellmapi:latest` | 1–3 min pull | |
| `stickerdaniel/linkedin-mcp-server:4.23.2` | 5–15 min pull | Large: Playwright + Chromium. |
| `hermes-core:latest` | 3–6 min | pip install of the pinned set. |
| `hermes-dashboard:latest` | 2–5 min | npm install + `vite build` + `tsc`. |
| `hermes-linkedin/sandbox:latest` | 10–25 min | LibreOffice is ~500–700 MB of it. |

**Working looks like:** `[build] done. Sandbox image tagged as
hermes-linkedin/sandbox:latest`, and `docker image ls` shows all five. The
sandbox build ends with a self-test — it imports `python-docx` and runs
`ats_docx.py --help` — so a broken pin fails `make build`, not a run at 2am.

To skip LibreOffice and lose only PDF output (saves ~500–800 MB):

```bash
docker compose --profile build-only build --build-arg INCLUDE_PDF=0 hermes-sandbox-image
```

With `INCLUDE_PDF=0`, `.docx` and `.txt` still generate; PDF download returns
`404` with an explanation.

### Step 3 — start (30–90 seconds to healthy)

```bash
make up
```

`hermes-core` waits for `freellmapi` to answer `/api/ping`
(`condition: service_healthy`, `start_period: 45s`) and for `linkedin-mcp` to
merely *start* — an unauthenticated LinkedIn session is a normal, recoverable
state that the dashboard explains.

**Working looks like:**

```bash
make ps          # all four services Up; freellmapi and hermes-core (healthy)
make health
```

`make health` probes `/api/health`, `freellmapi /api/ping`, the dashboard root
and `/api/linkedin/status`. At this point `/api/health` returns
`ok: true` (that flag tracks the **database only**, deliberately) with
`llm.key_configured: false` and `mcp.authenticated: false`.

`docker compose logs hermes-core` prints the startup banner, and this is where a
missing key is caught:

```
SETUP: FREELLMAPI_KEY is not set — open the freellmapi dashboard
(http://localhost:3001), create the local account, add at least one free
provider key, copy the 'freellmapi-...' token into .env, then restart
hermes-core. Until then every LLM call will fail with 401.
```

### Step 4 — mint the freellmapi token (5–10 minutes, manual)

This is the one value no script can produce. It is minted inside the router's own
dashboard.

1. Open **http://localhost:3001**.
2. Create the first local account. From a browser on this machine that is all
   that is needed; reached from **another device** it also wants a one-time setup
   code, printed in `docker compose logs freellmapi`.
3. Add one or more free provider keys. Available tiers include Google AI Studio,
   Groq, Cerebras, OpenRouter, Mistral, GitHub Models, SambaNova, Cohere, Z.ai,
   NVIDIA NIM and Cloudflare. **Two or three from *different* upstreams is the
   single highest-value tuning available** — free tiers rate-limit aggressively
   and the router fails over between them.
4. Mint / copy the unified client token. It looks like `freellmapi-…`.
5. Paste it into `.env` as `FREELLMAPI_KEY=` (keep the prefix).
6. ```bash
   make restart
   ```

`make restart` runs `docker compose up -d --force-recreate`. A bare
`docker compose restart` does **not** re-read `.env` — environment is fixed at
container-create time — so recreating is the only correct way to apply a new
token or model setting.

**Working looks like:**

```bash
curl -fsS http://127.0.0.1:8080/api/health
```

`llm.key_configured: true`, `llm.reachable: true`, `llm.models` > 0. The banner's
`configuration: complete` line replaces the `SETUP:` warning. Until this is done
every LLM call fails with 401 and `/api/health` says so.

`HERMES_MODEL_PRIMARY` can stay blank — `LLMRouter` auto-picks a chat-capable
model from `/v1/models`. Set it once you know which model your provider mix
actually serves reliably, and list two or three `HERMES_MODEL_FALLBACKS`.

### Step 5 — log into LinkedIn (2–5 minutes, manual, human-only)

```bash
make login-linkedin
```
```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\linkedin-login.ps1
```

Then open **http://localhost:6080/vnc.html** and sign in **with your own hands**:
email, password, 2FA code, and any captcha or checkpoint. Hermes never types your
password, never handles your 2FA code and never solves captchas. That is a design
decision, not a missing feature.

Leave the session on the LinkedIn feed, then stop the login container (`Ctrl-C`
in that terminal) and:

```bash
make restart
```

Prefer the scripts over the Makefile target here. Chromium takes an **exclusive
lock** on the browser profile, and `scripts/linkedin-login.*` stops
`linkedin-mcp` before starting the login container and restarts it afterwards.
`make login-linkedin` does not, so it can fail with a profile-lock error if
`linkedin-mcp` is already running — stop it first:

```bash
docker compose stop linkedin-mcp
```

Script flags: `--keep-running` / `-KeepRunning` (do not touch `linkedin-mcp`),
`--no-restart` / `-NoRestart` (stop it and leave it down), `--yes` (shell only).

**Working looks like:**

```bash
curl -fsS "http://127.0.0.1:8080/api/linkedin/status?refresh=1"
```

`reachable: true`, `authenticated: true`, `login_required: false`, and `tools`
listing 19 entries. A first check right after start can legitimately return
`status: "checking"` with HTTP 200 — a cold container needs up to ~90 s to open
Chromium and load the top card. Poll again.

Without a session, tool calls fail with:

> `No valid LinkedIn session is available in Docker. Create one with the explicit --login --login-viewer Docker command, or run --login on the host, then retry this tool.`

The dashboard surfaces this as **"LinkedIn not connected"**.

### Step 6 — first real run (1–8 minutes)

Open **http://localhost:3000**. Overview → import your profile, then search
jobs. Or by API:

```bash
curl -fsS -X POST http://127.0.0.1:8080/api/profile/import \
  -H 'content-type: application/json' -d '{}'
```

That returns a `Run` immediately with `status: "pending"`. Follow it:

```bash
curl -N http://127.0.0.1:8080/api/runs/<run_id>/events
```

**Expected timings** — these are why runs are asynchronous and streamed rather
than returned inline:

| Operation | Duration |
|---|---|
| profile scrape (`get_my_profile`) | **20–90 s** (up to ~90 s from cold) |
| `search_jobs`, `max_pages=1` | 30–90 s |
| `search_jobs`, `max_pages=10` | **can exceed five minutes** |
| MatchRanker, per job | 2–15 s, run at concurrency 1–8 (default 3) |
| resume build + render + ATS score | 30–120 s (LibreOffice PDF is the slow part) |
| deterministic ATS score alone | milliseconds |

MCP tool calls are **serialised through a semaphore (default concurrency 1)**:
one browser, one tab. Concurrent calls would interleave navigation and corrupt
each other's scrapes, so anything that looks parallel simply queues.

**Working looks like:** the run reaches `status: "done"`, the Jobs page lists
postings sorted by `match_score`, and each row carries an apply link.

### Step 7 — apply

You click the link. You read the posting. You press Submit.

**Hermes never submits an application.** The MCP server exposes no apply tool and
Hermes adds none — it does not fill forms, click Submit, send Easy Apply
requests, or message recruiters. When you have applied, tell Hermes so it can
stamp `applied_at`:

```bash
curl -fsS -X PATCH http://127.0.0.1:8080/api/jobs/<job_id> \
  -H 'content-type: application/json' -d '{"status":"applied"}'
```

Nothing else in Hermes writes that field.

Before you rely on any of this, read
[LICENSE-NOTICE.md](../LICENSE-NOTICE.md) — running an automated client against
LinkedIn can violate the User Agreement, and the account at risk is yours.

---

## 2. Day-2 operations

### Check health

```bash
make health
```

Individually:

```bash
curl -fsS http://127.0.0.1:8080/api/health              # hermes-core + deps
curl -fsS http://127.0.0.1:3001/api/ping                # LLM router
curl -fsS -o /dev/null -w '%{http_code}\n' http://127.0.0.1:3000/
curl -fsS http://127.0.0.1:3000/healthz                 # nginx itself
curl -fsS http://127.0.0.1:8080/api/linkedin/status
```

Reading `/api/health`:

| Field | Meaning |
|---|---|
| `ok` | **The database only.** Deliberately independent of LLM config and LinkedIn login, so the container HEALTHCHECK does not restart-loop over an un-configured dependency. |
| `llm.reachable`, `llm.key_configured`, `llm.models`, `llm.primary` | Router state. `key_configured: false` ⇒ do Step 4. |
| `mcp.reachable`, `mcp.authenticated`, `mcp.tools` | Scraper state. `reachable: true` + `authenticated: false` ⇒ re-login. |
| `docker` (bool) + `docker_detail` | Socket reachability. `false` ⇒ the Containers page and sandbox return 503; check the `/var/run/docker.sock` mount. |
| `db` | SQLite probe. |

Each probe has its own timeout (8/10/8 s) and its own try/except: a dead router
never stops the response reporting that MCP is fine.

Container-level view:

```bash
make ps
make logs S=hermes-core
docker compose logs --tail=200 freellmapi
```

The interactive API docs are at **http://127.0.0.1:8080/api/docs**.

### Read a failed run

```bash
curl -fsS 'http://127.0.0.1:8080/api/runs?status=error&limit=20'
curl -fsS http://127.0.0.1:8080/api/runs/<run_id>          # includes the event log
```

Or the Runs page in the dashboard, which streams the same events live.

What to look at, in order:

1. **`Run.error`** — `"<ExceptionType>: message"`, truncated to 4000 chars. This
   is the single most useful field.
2. **`Run.result`** — partial results are preserved on failure. For
   `full_pipeline`, `result["stages"]` shows exactly how far it got
   (`profile_import` → `resume_build` → `job_search` → `tailored`).
3. **The event log** — `RunEvent` rows, levels `debug|info|warn|error|end`. The
   last `error` line before the terminal event is where it broke. Replay is
   capped at 500 events.
4. **`docker compose logs hermes-core`** — the API's 500 envelope deliberately
   never leaks a traceback to the browser but always logs one. Its `detail` field
   tells you to look here.

Common causes and where they surface:

| Symptom in the run | Cause | Fix |
|---|---|---|
| `ConfigError: LLM router not configured: missing FREELLMAPI_KEY` | Token never set, or set without recreating containers | Step 4, then `make restart` |
| `LLMAllModelsFailed` with 429s | Every provider tier rate-limited | Add another provider (below), or lower `rank_concurrency` |
| `MCPAuthError` / text about login/credentials/session | LinkedIn session expired | Re-login (below) |
| `MCPUnavailableError` | `linkedin-mcp` down or wrong URL | `make ps`; `LINKEDIN_MCP_URL` must be `http://linkedin-mcp:8000/mcp` |
| `SandboxUnavailableError: … image … not found` | Sandbox image never built | `make build` |
| `SandboxUnavailableError: Docker refused to start…` | Workspace bind source does not exist on the daemon host | See `HERMES_SANDBOX_HOST_WORKSPACE` in ARCHITECTURE.md §7 |
| `Run cancelled (service shutting down or task cancelled).` | Container stopped mid-run | Re-run it; nothing is corrupt |
| PDF download `404` | Sandbox image built with `INCLUDE_PDF=0`, or LibreOffice absent | Use `.docx`/`.txt`, or rebuild without the build arg |

### Re-log into LinkedIn

Expired sessions are normal — LinkedIn invalidates them, and a checkpoint or
password change kills them immediately.

```bash
# Confirm it is really an auth problem, not an unreachable container:
curl -fsS "http://127.0.0.1:8080/api/linkedin/status?refresh=1"
```

`reachable: true` + `authenticated: false` + `login_required: true` ⇒ re-login:

```bash
bash scripts/linkedin-login.sh
```
```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\linkedin-login.ps1
```

Open `http://localhost:6080/vnc.html`, sign in by hand, `Ctrl-C`, then
`make restart`. Verify with `?refresh=1` — the status endpoint caches results for
45 s, and `POST /api/linkedin/login` also invalidates that cache.

If the viewer port is already taken by a leftover container:

```bash
docker compose --profile login rm -f linkedin-login
```

`GET /api/linkedin/status` never re-probes on the request path for longer than a
couple of seconds; a cold probe keeps running in the background and lands in the
cache for the next poll. `status: "checking"` is not an error.

### Rotate the freellmapi token

Do this if the token leaks, or on a schedule.

1. Open `http://localhost:3001`, mint a new client token, revoke the old one.
2. Edit `FREELLMAPI_KEY` in `.env`.
3. ```bash
   make restart
   ```
4. ```bash
   curl -fsS http://127.0.0.1:8080/api/health   # llm.key_configured / llm.models
   curl -fsS -X POST http://127.0.0.1:8080/api/llm/test \
     -H 'content-type: application/json' -d '{"prompt":"say hi"}'
   ```

`make restart` (force-recreate) is required — `docker compose restart` will not
pick up the new value. A key that does not start with `freellmapi-` is accepted
but logs a warning, because the router will probably reject it.

**Do not rotate `ENCRYPTION_KEY`.** It is not a session secret; it is what
`freellmapi` encrypts its stored provider keys with. Changing it makes every
stored provider key undecryptable and you have to re-add them all in the
dashboard. Keep it in a password manager, alongside your backups.

### Add a provider when one rate-limits

Symptom: runs succeed but slowly, `RunEvent` lines mention a fallback model, or
runs fail with `LLMAllModelsFailed` listing 429s per model.

1. Open `http://localhost:3001` and add a key from a **different** upstream than
   the one that is throttling. `LLMRouter` walks primary → fallbacks →
   auto-picked, with 3 attempts per model, exponential backoff with jitter, and
   it honours `Retry-After` (capped at 60 s).
2. Optionally pin the chain in `.env`:
   ```
   HERMES_MODEL_PRIMARY=llama-3.3-70b-versatile
   HERMES_MODEL_FALLBACKS=gemini-2.0-flash,mistral-small-latest
   ```
   then `make restart`. Model ids must match `GET /v1/models` exactly:
   ```bash
   curl -fsS http://127.0.0.1:8080/api/llm/models
   ```
3. Or change it live without a restart — the Settings page writes
   `model_primary` / `model_fallbacks` into the `setting` table and the pipeline
   applies them per run:
   ```bash
   curl -fsS -X PUT http://127.0.0.1:8080/api/settings \
     -H 'content-type: application/json' \
     -d '{"model_primary":"gemini-2.5-flash","model_fallbacks":"llama-3.3-70b-versatile"}'
   ```
4. Reduce pressure instead of adding capacity:
   * lower `rank_concurrency` (clamped 1–8, default 3);
   * lower `job_search_max_pages` (default 3 — `max_pages=10` also raises the
     LinkedIn flagging risk);
   * `MAX_RANKED_PER_RUN = 120` already caps ranking calls per search.

The model that actually served each call is logged and emitted into the run's
event stream, so the dashboard shows reality rather than configuration.

### Clear stuck runs

A run can only be left at `running` if `hermes-core` died without draining —
normal shutdown calls `shutdown_runs()`, which cancels in-flight tasks so they
record a terminal status.

```bash
curl -fsS 'http://127.0.0.1:8080/api/runs?status=running&limit=50'
```

There is **no API to cancel or delete a run** — that is not in the 29-path
surface. Options, cheapest first:

1. **Wait.** A genuinely running MCP call can legitimately take five-plus
   minutes.
2. **Recreate core.** In-flight tasks are cancelled and recorded as errors:
   ```bash
   docker compose up -d --force-recreate hermes-core
   ```
3. **Fix the rows by hand**, for runs orphaned by a hard kill:
   ```bash
   make shell-core
   python - <<'PY'
   from hermes.db import session_scope
   from hermes.models import Run, utcnow
   with session_scope() as db:
       stale = db.query(Run).filter(Run.status.in_(("pending", "running"))).all()
       for r in stale:
           r.status = "error"
           r.error = "Orphaned by a hermes-core restart; marked failed by an operator."
           r.finished_at = utcnow()
       print("marked", len(stale))
   PY
   ```
   Deleting a `Run` row cascades to its `run_event` and `sandbox` rows
   (`foreign_keys=ON` is set per connection, so the cascades are live).

### Prune sandbox containers and workspaces

Sandbox containers are created with `auto_remove=False` so the exit code, logs
and artifacts can be collected deterministically. Exited ones therefore
accumulate.

```bash
docker ps -a --filter label=hermes.role=sandbox
docker container prune -f --filter label=hermes.role=sandbox
```

The equivalent is available in-process (`SandboxManager.prune_sandbox_containers`
filters `hermes.role=sandbox` + `status=exited`), and the Containers page can
remove them individually.

Per-run workspaces live in the `hermes-data` volume at `/data/workspaces/<run_id>`:

```bash
docker compose exec hermes-core du -sh /data/workspaces /data/renders /data/resumes /data/uploads
docker compose exec hermes-core sh -c 'ls /data/workspaces | head'
```

`/data/renders/<name>-<stamp>/` holds the durable copies referenced by
`Resume.docx_path` / `pdf_path` / `txt_path` — **do not delete those** while the
resume rows still matter. Workspaces are safe to remove once their runs are
terminal.

Note that stop/restart/remove of `hermes-core` itself is refused with `409` by
the API — that would tear down the process serving the request. Use
`docker compose` from the host.

### Back up

```bash
bash scripts/backup.sh
```
```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\backup.ps1
```

Writes `dist/backups/hermes-backup-<yyyyMMdd-HHmmss>/` containing
`hermes-data.tar.gz`, `freellmapi-data.tar.gz` and a `MANIFEST.txt`. By default
it stops the containers first for a consistent SQLite snapshot and restarts them
afterwards. Flags: `--out DIR`, `--project NAME`, `--helper-image IMG`, `--live`
(do not stop the stack — the DB may be captured mid-WAL-checkpoint; not
recommended for a migration you care about), `--no-restart`, `--force`.

`linkedin-session` is **never** backed up, and no flag overrides that. It is a
live authenticated browser session; see ARCHITECTURE.md §8.

**Record `ENCRYPTION_KEY` separately**, in a password manager — not next to the
backup. `.env` is not in the archive (it holds secrets, and backups get emailed
around), and `freellmapi-data` restored without the matching key leaves
undecryptable provider keys.

Treat the backup folder like a password file: it contains your scraped LinkedIn
profile, your resumes, and your encrypted provider keys.

> Prefer the scripts over `make backup` / `make restore`. Those Makefile targets
> compute volume names from `PROJECT := hermes`, which does not match the pinned
> compose project `hermes-linkedin`, and they include `linkedin-session`.

### Restore

The stack must be down first — restore refuses to overwrite volumes while
containers are running.

```bash
make down
bash scripts/restore.sh --from dist/backups/hermes-backup-20260902-101500
```
```powershell
.\scripts\restore.ps1 -From dist\backups\hermes-backup-20260902-101500
```

With no `--from` / `-From` it picks the newest `hermes-backup-*` directory under
`dist/backups`. It verifies the archives exist and look like a Hermes backup
before touching anything, and prompts before overwriting a non-empty volume
(`--force` / `-Force` skips the prompts). It **refuses** to restore
`linkedin-session` even if an archive for it is present. Other flags:
`--project NAME`, `--helper-image IMG`, `--down` (bring the stack down for you
instead of refusing), `--dry-run`.

Afterwards:

```bash
# ENCRYPTION_KEY in .env must match the one from the source machine,
# or re-add your provider keys at http://localhost:3001
make up
make health
bash scripts/linkedin-login.sh    # the session is never transferred
```

### Upgrade the pinned images

`linkedin-mcp-server` is pinned to `4.23.2` and is the risky one: it drives a
real browser against a moving DOM, and the MCP protocol/SDK pairing is verified
(mcp 2.1.1 ↔ 4.23.2, protocol 2025-11-25, 19 tools).

```bash
# 1. Back up first. Non-negotiable.
bash scripts/backup.sh

# 2. Change the tag in docker-compose.yml (two places: linkedin-mcp and
#    linkedin-login — they MUST stay identical; they share the session volume).
#    Also update IMAGES in the Makefile and the version in LICENSE-NOTICE.md.

# 3. Pull and recreate.
docker compose pull linkedin-mcp
docker compose up -d --force-recreate linkedin-mcp

# 4. Verify the tool surface before trusting a run.
curl -fsS "http://127.0.0.1:8080/api/linkedin/status?refresh=1"
```

Check `tools` still contains the five Hermes uses — `get_my_profile`,
`get_person_profile`, `search_jobs`, `get_job_details`, `get_saved_jobs` — and
that `search_jobs` still accepts `keywords`, `location`, `max_pages`,
`date_posted`, `job_type`, `experience_level`, `work_type`, `easy_apply`,
`sort_by`. Then run one small search (`max_pages=1`) and read the run's events.
Roll back by restoring the old tag and recreating; the session volume is
untouched by an image change.

`freellmapi` tracks `:latest` by upstream convention:

```bash
docker compose pull freellmapi
docker compose up -d freellmapi
curl -fsS http://127.0.0.1:3001/api/ping
```

Pin it to a digest if you need reproducible builds. Its data volume and your
`ENCRYPTION_KEY` carry the provider keys across the upgrade.

First-party images after a `git pull`:

```bash
make build
make up
make health
```

The core has **no schema migrations** — `init_db()` is create-only
(`create_all`). A release that changes a column requires either a hand-written
`ALTER TABLE` inside `make shell-core` or a reset. Read the release notes before
upgrading, and always back up first.

For an offline/air-gapped move, use the ship/load pair — bundle on the source
machine, load on the target — and then restore volumes separately:

```bash
bash scripts/ship.sh --mode images      # ~3-6 GB, 5-15 min
# ...copy the bundle...
bash scripts/load.sh                    # docker load + up, no rebuild
bash scripts/restore.sh --from <backup dir>
bash scripts/linkedin-login.sh
```

`--mode source` produces a ~1–5 MB repo copy instead and rebuilds on arrival
(needs internet and 5–15 minutes). Neither mode includes `.env` or any volume.

### Reset all state

Destructive. Deletes containers **and** all three volumes: the SQLite DB, every
generated resume, your stored provider keys, and your LinkedIn login.

```bash
make reset          # prompts; type YES
```

That runs `docker compose --profile build-only --profile login down -v
--remove-orphans`. `ENCRYPTION_KEY` in `.env` survives (the file is untouched),
but it is now meaningless because the `freellmapi-data` volume it decrypted is
gone.

Afterwards you are back at Step 3 and must redo Step 4 (provider keys + token)
and Step 5 (LinkedIn login). Images are kept, so there is no rebuild.

Less destructive alternatives:

```bash
make down       # stop and remove containers; volumes and data KEPT
make clean      # remove ./dist and prune dangling images; volumes untouched
```

Reset one volume only (stack down first):

```bash
make down
docker volume rm hermes-linkedin_freellmapi-data    # forget provider keys only
docker volume rm hermes-linkedin_linkedin-session   # force a fresh LinkedIn login
make up
```

---

## 3. Quick reference

| Task | Command |
|---|---|
| Create `.env` + key | `make bootstrap` |
| Full preflight + build + up | `scripts/bootstrap.ps1` / `bootstrap.sh` |
| Build all images | `make build` |
| Start / stop | `make up` / `make down` |
| Apply `.env` changes | `make restart` |
| Status | `make ps` |
| Logs (one service) | `make logs S=hermes-core` |
| Health of everything | `make health` |
| Shell in core | `make shell-core` |
| LinkedIn login | `scripts/linkedin-login.ps1` / `.sh` |
| Back up volumes | `scripts/backup.ps1` / `.sh` |
| Restore volumes | `scripts/restore.ps1` / `.sh` |
| Offline bundle / install | `scripts/ship.*` / `scripts/load.*` |
| Delete everything | `make reset` |

| URL | What |
|---|---|
| http://localhost:3000 | Dashboard |
| http://localhost:8080/api/health | Core health |
| http://localhost:8080/api/docs | OpenAPI docs |
| http://localhost:3001 | freellmapi dashboard (provider keys, token) |
| http://localhost:6080/vnc.html | LinkedIn login viewer (only while the one-shot runs) |
