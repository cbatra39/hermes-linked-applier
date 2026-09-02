# Hermes — LinkedIn job scout, ATS resume builder, self-hosted

Hermes is a five-container Docker stack you run on your own laptop. It logs into
LinkedIn as *you* (in a browser you drive by hand, once), reads your own profile,
rewrites your resume so an applicant-tracking system can actually parse it, scores that
resume against a deterministic ATS heuristic, searches and ranks job postings against
your profile, and presents the result as a dashboard of ranked jobs with a match
breakdown, a tailored resume per posting, and a plain apply link. **Hermes never submits
an application.** There is no apply tool in the stack — not disabled, not gated, absent.
The upstream LinkedIn MCP server exposes none and Hermes adds none. It produces ranked
jobs + tailored resumes + `https://www.linkedin.com/jobs/view/<id>/` links; you open the
link, read the posting, and press Submit yourself. See
[LICENSE-NOTICE.md](LICENSE-NOTICE.md) for why that boundary is deliberate and permanent.

---

## Architecture

Compose project name is pinned to **`hermes-linkedin`**, so the network is
`hermes-linkedin_hermes` and volumes are prefixed `hermes-linkedin_`.

```
       YOU  -  a browser on this machine
        |          |             |              |
     :3000       :8080         :3001          :6080
   dashboard   API direct    router UI    noVNC (login only)
        |          |             |              |
==== HOST LOOPBACK - all ports bind to HOST_BIND, default 127.0.0.1 ====
==== docker network:  hermes-linkedin_hermes ===========================
        |
        v
+----------------------------------------------------------+
| hermes-dashboard                      :3000 -> :80       |
| nginx + React / Vite / TypeScript SPA                    |
| reverse-proxies /api -> hermes-core:8080, SSE-safe       |
+----------------------------------------------------------+
        |
        |   /api  (single origin, no CORS, unbuffered streams)
        v
+----------------------------------------------------------+
| hermes-core                                     :8080    |
| FastAPI, 31 endpoints under /api                         |
| profile analyst | resume architect | ATS scorer          |
| job scout | match ranker | run + SSE events              |
| SQLite DB | sandbox manager                              |
+----------------------------------------------------------+
  |
  +--> OpenAI-compatible, POST /v1/...
  |
  |    +----------------------------------------------------------+
  |    | freellmapi                                      :3001    |
  |    | self-hosted OpenAI-compatible router                     |
  |    | ~34 free LLM provider tiers, automatic failover          |
  |    | its own dashboard is where YOU add provider keys         |
  |    | and mint the client token (freellmapi-...)               |
  |    +----------------------------------------------------------+
  |
  +--> MCP streamable-http, POST /mcp
  |
  |    +----------------------------------------------------------+
  |    | linkedin-mcp                        no published port    |
  |    | 19 tools, protocol 2025-11-25.   NO APPLY TOOL.          |
  |    | get_my_profile, get_person_profile, search_jobs,         |
  |    | get_job_details, get_saved_jobs                          |
  |    | drives a REAL headless Chromium, as YOUR account         |
  |    +----------------------------------------------------------+
  |         ^ shares the browser-profile volume with:
  |         |
  |    +----------------------------------------------------------+
  |    | linkedin-login          :6080, profiles: [login]         |
  |    | ONE-SHOT, human-only. You sign in by hand at             |
  |    | http://127.0.0.1:6080/vnc.html -- password, 2FA,         |
  |    | captcha. Never started by a plain `up`.                  |
  |    +----------------------------------------------------------+
  |
  +--> mounts /var/run/docker.sock == HOST-ROOT-EQUIVALENT ACCESS
  |    drives the Containers page, and spawns per render/exec:
  |
  |    +----------------------------------------------------------+
  |    | hermes-linkedin/sandbox:latest  EPHEMERAL, not a service |
  |    | built via `--profile build-only`; lives seconds, is gone |
  |    | network_disabled, read-only rootfs, tmpfs /tmp,          |
  |    | cap_drop ALL, no-new-privileges, pids_limit 256,         |
  |    | mem/cpu capped, user 1000:1000, workspace at /work       |
  |    | LibreOffice inside, for the optional .docx -> .pdf step  |
  |    +----------------------------------------------------------+

==== NAMED VOLUMES  (real names carry the hermes-linkedin_ prefix) =====

+----------------------------------------------------------+
| hermes-data       ->  hermes-core:/data                  |
| hermes.db (SQLite), resumes/, uploads/, workspaces/      |
+----------------------------------------------------------+
+----------------------------------------------------------+
| freellmapi-data   ->  freellmapi:/app/server/data        |
| provider keys, encrypted with ENCRYPTION_KEY             |
+----------------------------------------------------------+
+----------------------------------------------------------+
| linkedin-session  ->  /home/pwuser/.linkedin-mcp         |
| the authenticated browser profile == YOUR cookies        |
| written by linkedin-login, read by linkedin-mcp          |
| *** NEVER copy between machines. Log in again. ***       |
+----------------------------------------------------------+
```

| Service | Image | Host port | Volume | Role |
|---|---|---|---|---|
| `freellmapi` | `ghcr.io/tashfeenahmed/freellmapi:latest` | 3001 | `freellmapi-data` | OpenAI-compatible router stacking ~34 free LLM provider tiers behind one key, with failover. Its dashboard is where provider keys go and the client token is minted. |
| `linkedin-mcp` | `stickerdaniel/linkedin-mcp-server:4.23.2` | *(none)* | `linkedin-session` | 19 LinkedIn tools over MCP streamable-http at `:8000/mcp`. Drives a real headless Chromium logged into your account. |
| `linkedin-login` | same image, `profiles: ["login"]` | 6080 | `linkedin-session` | One-shot interactive sign-in with a noVNC viewer. Not started by a plain `up`. |
| `hermes-core` | built, `hermes-core:latest` | 8080 | `hermes-data` + `docker.sock` | FastAPI orchestrator: 31 endpoints under `/api`, SQLite, agents, run/SSE event system, sandbox manager. |
| `hermes-dashboard` | built, `hermes-dashboard:latest` | 3000 → 80 | — | React + Vite + TS behind nginx; reverse-proxies `/api` to `hermes-core:8080` (single origin, SSE-safe). |
| `hermes-sandbox-image` | built, `hermes-linkedin/sandbox:latest`, `profiles: ["build-only"]` | — | — | Not a service. Build target only; `hermes-core` spawns containers **from** it. |

---

## Steps that need YOUR interaction

Ordered, start to finish, on a fresh laptop. Steps 4, 5 and 7 cannot be automated —
they need a human account, a human password, and a human clicking "I accept".

### 1. Install Docker Desktop — 10–20 min including a reboot

Install Docker Desktop (Windows/macOS) or Docker Engine + Compose v2 (Linux), start it,
and wait for the whale icon to stop animating.

```powershell
docker info --format '{{.ServerVersion}}'
docker compose version --short
```

**Worked when:** both print a version. Compose must be v2 (the `docker compose`
subcommand, not the legacy `docker-compose` binary) — Hermes' scripts refuse to
continue otherwise.

### 2. Get the project onto the machine — 1 min

Copy or clone this directory. From here on, every command runs from the project root
(the directory holding `docker-compose.yml`).

```powershell
cd C:\Users\<you>\Documents\hermes-linkedin-applier
```

### 3. Bootstrap: config, build, start — 5–15 min (first build)

The bootstrap script does preflight (Docker up? Compose v2? ports free?), creates `.env`
from `.env.example`, generates `ENCRYPTION_KEY`, builds `hermes-core`, `hermes-dashboard`
and the build-only sandbox image, then starts the stack.

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\bootstrap.ps1
```

```bash
./scripts/bootstrap.sh          # Linux / macOS / Git Bash
# or, if you have make:
make bootstrap && make build && make up
```

**You will see:** a preflight checklist, a long build (Playwright/Chromium and
LibreOffice are the slow parts), `docker compose ps`, then a bright
"WHAT YOU MUST DO BY HAND" block. Note that the script deliberately **holds `hermes-core`
back** while `FREELLMAPI_KEY` is blank — it starts only `freellmapi`, `linkedin-mcp` and
`hermes-dashboard`. (`-All` / `--all` overrides this; `make up` starts everything
regardless.)

**Worked when:** `.env` exists with a 64-hex `ENCRYPTION_KEY`, and:

```powershell
curl.exe http://127.0.0.1:3001/api/ping      # freellmapi alive
curl.exe http://127.0.0.1:3000/healthz       # dashboard -> ok
```

Useful flags: `-SkipBuild` / `--skip-build` (images already built or `docker load`ed),
`-Pull` / `--pull`, `-NoCache` / `--no-cache`, `-Force` / `--force` (port conflicts
become warnings).

### 4. Create the freellmapi account and add free provider keys — 5–15 min, by hand

Open **http://127.0.0.1:3001** and create the first local account. This account lives
inside your own container; it is not registered with anyone. A browser on the *same*
machine needs nothing extra. Reached from **another device** on your LAN, freellmapi
prints a one-time setup code to its log instead of trusting the browser:

```powershell
docker compose logs freellmapi
```

Then add at least one — preferably three or four — free provider keys. Each is free to
create and independently rate-limited, so a mix is what makes failover work:

> Google AI Studio · Groq · Cerebras · OpenRouter · Mistral · GitHub Models · SambaNova ·
> Cohere · Z.ai · NVIDIA NIM · Cloudflare

**Worked when:** the router's model list is non-empty (its Settings page, or
`GET /v1/models`).

### 5. Paste the router token into `.env` — 2 min, by hand

In the same dashboard, mint/copy the unified **client token**. It looks like
`freellmapi-xxxxxxxx`. Put it in `.env`, keeping the prefix:

```dotenv
FREELLMAPI_KEY=freellmapi-xxxxxxxxxxxxxxxx
```

This is the **only** value you must fill in by hand. It cannot be generated offline —
it only exists once the router has minted it.

### 6. Recreate `hermes-core` so it reads the new token — under 1 min

A bare `docker compose restart` does **not** re-read `.env`; environment is fixed at
container-create time. Recreate:

```powershell
docker compose up -d hermes-core        # first start, if bootstrap held it back
docker compose up -d --force-recreate   # or recreate the whole stack
```

```bash
make restart
```

**Worked when** `/api/health` reports the LLM as usable:

```powershell
curl.exe http://127.0.0.1:8080/api/health
```

Look for `llm.ok` true and `llm_key_present` true. Until the token is set, `hermes-core`
still starts and job search plus the deterministic ATS scorer still work, but every
LLM-backed feature (profile analysis, resume writing, semantic ATS pass, match ranking)
fails with a configuration error naming `FREELLMAPI_KEY`.

### 7. Log into LinkedIn by hand at the noVNC viewer — ~1–2 min, once per machine

This is interactive and human-only. Hermes does not type your credentials, does not
store them, and cannot solve a captcha.

```powershell
.\scripts\linkedin-login.ps1
```

```bash
./scripts/linkedin-login.sh
# or:
make login-linkedin
```

The script stops `linkedin-mcp` first — Chromium takes an exclusive lock on the browser
profile directory and the two containers cannot share it live — then starts the one-shot
`linkedin-login` service, which publishes noVNC on `LINKEDIN_VIEWER_PORT`.

**You will do:** open **http://127.0.0.1:6080/vnc.html**, press Connect (no VNC password
is set), and in the remote browser sign in yourself: email, password, 2FA one-time code,
and any captcha or "is this you?" challenge. Land on your LinkedIn feed — that is what
"logged in" means here. The container detects the session, writes the browser profile to
the `linkedin-session` volume, and exits; if it does not exit on its own, press Ctrl+C
once you are on the feed. The script then restarts `linkedin-mcp` for you.

**Worked when:**

```powershell
curl.exe http://127.0.0.1:8080/api/linkedin/status
```

returns `{"reachable": true, "authenticated": true, ...}`. With no session, tool calls
fail with the MCP server's own message —

> No valid LinkedIn session is available in Docker. Create one with the explicit
> `--login --login-viewer` Docker command, or run `--login` on the host, then retry this
> tool.

— which the dashboard surfaces as **"LinkedIn not connected"**.

### 8. Open the dashboard — instant

**http://127.0.0.1:3000**

Pages: Overview · Jobs · Resume · LinkedIn · Containers · Runs · Settings.

**Worked when:** Overview shows LLM, LinkedIn and Docker all green.

### 9. Import your profile — 1–3 min per run

**LinkedIn page → Import profile.** This drives a real browser through real page loads
and scrolling (`TOOL_TIMEOUT` is set to 180s for exactly this reason), then runs the
profile-analyst agent over the result.

**Worked when:** the Runs page shows the run as `done` and the LinkedIn page shows your
parsed profile with its analysis. Watch it live — the run log is an SSE stream.

### 10. Generate a resume — 1–3 min

**Resume page → Generate.** Optionally upload a base resume first
(`.pdf`, `.docx`, `.txt`, `.md`, up to 25 MB) so the agent works from what you already
have. Rendering happens in an ephemeral sandbox container, not in `hermes-core`.

**Worked when:** the resume appears with an ATS score and a subscore breakdown, and the
download menu offers `md` (always), `docx`, `txt`, and `pdf` (only if LibreOffice is in
the sandbox image).

### 11. Search and rank jobs — 1–5 min depending on `max_pages`

**Jobs page → Search.** `keywords` is required; `location`, `max_pages` (1–10),
`date_posted`, `job_type`, `experience_level`, `work_type`, `easy_apply` and `sort_by`
are optional. Keep `max_pages` modest — volume is what gets LinkedIn accounts flagged.

**Worked when:** the Jobs table fills, best match first, each row carrying a match score
and a breakdown.

### 12. Open apply links yourself and mark status — ongoing, entirely manual

Open a job, optionally hit **Tailor** to build a resume aimed at that one posting, then
click the apply link. **You** read the posting and press Submit — Hermes does not and
will not. Come back and set the job's status so the list stays useful:

`new` · `shortlisted` · `tailored` · `applied` · `rejected` · `skipped`

---

## Quick reference

### URLs

| URL | What |
|---|---|
| http://127.0.0.1:3000 | Dashboard (the one you use daily) |
| http://127.0.0.1:3000/healthz | nginx liveness → `ok` |
| http://127.0.0.1:8080/api/health | Core + dependency health |
| http://127.0.0.1:8080/api/docs | OpenAPI docs for all 31 endpoints |
| http://127.0.0.1:8080/api/linkedin/status | Session state |
| http://127.0.0.1:3001 | freellmapi dashboard (provider keys, token, models) |
| http://127.0.0.1:3001/api/ping | Router liveness |
| http://127.0.0.1:6080/vnc.html | noVNC login viewer (only while `linkedin-login` runs) |

### `make` targets

Every target is a thin wrapper — read the recipe and run the `docker compose` line by
hand if you prefer. Ports are overridable variables: `make health API_PORT=9090`.
Overridable: `API_PORT` (8080), `DASHBOARD_PORT` (3000), `LLM_PORT` (3001),
`VIEWER_PORT` (6080), `S` (service filter).

| Target | Does |
|---|---|
| `make help` | Print targets and the first-run sequence (default target) |
| `make bootstrap` | Copy `.env.example` → `.env`, generate `ENCRYPTION_KEY`, create `dist/`. Never overwrites an existing `.env` |
| `make build` | Pull the two pinned upstream images, then build core, dashboard **and** the build-only sandbox image |
| `make up` | `docker compose up -d`, then print the URLs |
| `make down` | Stop and remove containers; **volumes and data are kept** |
| `make restart` | `up -d --force-recreate` — the only correct way to apply a changed `.env` |
| `make logs` | Follow logs, `--tail=200`. One service: `make logs S=hermes-core` |
| `make ps` | Container status (includes the build-only and login profiles) |
| `make login-linkedin` | Start the one-shot login container via `--profile login up --abort-on-container-exit` |
| `make shell-core` | Shell inside `hermes-core` (`bash`, falling back to `sh`) |
| `make health` | Curl core `/api/health`, router `/api/ping`, dashboard `/`, and `/api/linkedin/status` |
| `make reset` | **DESTRUCTIVE.** `down -v` — deletes DB, resumes, provider keys and your LinkedIn session. Prompts for `YES` |
| `make ship` | `docker save` all five images to `dist/hermes-images.tar` + `dist/hermes-repo.tar.gz` (excludes `.env`) |
| `make load` | `docker load -i dist/hermes-images.tar` |
| `make backup` | Stream all three volumes to `dist/hermes-volumes.tar.gz` via a throwaway `alpine:3.20` container |
| `make restore` | Restore from `dist/hermes-volumes.tar.gz`; brings the stack down first, prompts for `YES` |
| `make clean` | Remove `dist/`, bring the stack down, prune dangling images. Volumes untouched |

`make` is **not** installed with Docker Desktop. On Windows either install it
(`choco install make`), run the targets from Git Bash, or use the scripts below.

### Scripts

Each exists as a `.ps1` (PowerShell 5.1+) and a `.sh` (bash 4+ / Git Bash) pair with
matching behaviour. There is no `up`/`down`/`health`/`restore` script — use
`docker compose` or `make` for those.

| Script | Flags | Does |
|---|---|---|
| `scripts/bootstrap.ps1` / `.sh` | `-SkipBuild` `-SkipPreflight` `-Force` `-All` `-Pull` `-NoCache` (`--skip-build` `--skip-preflight` `--force` `--all` `--pull` `--no-cache`) | Preflight → `.env` → build → up → the manual checklist. Safe to re-run |
| `scripts/linkedin-login.ps1` / `.sh` | `-KeepRunning` `-NoRestart` (`--keep-running` `--no-restart` `--yes`) | Stop `linkedin-mcp`, run the login container with the viewer published, restart `linkedin-mcp`, print how to verify |
| `scripts/ship.ps1` / `.sh` | `-Mode images\|source` `-OutDir` `-Force` `-Zip` (`--mode` `--out` `--force` `--zip`) | Build a portable bundle in `dist/`. Never includes `.env`, the DB, or `linkedin-session` |
| `scripts/load.ps1` / `.sh` | `-TarPath` `-Project` `-SkipLoad` `-All` `-NoUp` (`--tar` `--project` `--skip-load` `--all` `--no-up`) | Receiving half of `ship`: locate the tar, `docker load`, verify all five images, create `.env`, `up -d --no-build` |
| `scripts/backup.ps1` / `.sh` | `-OutDir` `-Project` `-HelperImage` `-Live` `-NoRestart` `-Force` (`--out` `--project` `--helper-image` `--live` `--no-restart` `--force`) | Archive `hermes-data` and `freellmapi-data` to separate `.tar.gz` files. **Refuses to back up `linkedin-session`**, and `-Force` does not change that |

### Environment variables (`.env`)

`make bootstrap` / the bootstrap scripts create `.env` from `.env.example` and generate
`ENCRYPTION_KEY`. **Exactly one value needs a human: `FREELLMAPI_KEY`.**

| Variable | Default | Who fills it |
|---|---|---|
| `HOST_BIND` | `127.0.0.1` | default — `0.0.0.0` publishes an unauthenticated stack |
| `ENCRYPTION_KEY` | *(empty)* | **auto** (bootstrap): 64 hex chars; freellmapi encrypts its provider-key store with it |
| `FREELLMAPI_BASE_URL` | `http://freellmapi:3001/v1` | fixed — in-network; `localhost` here means `hermes-core` |
| `FREELLMAPI_KEY` | *(empty)* | **YOU, BY HAND** — minted in the router dashboard, `freellmapi-...` |
| `FREELLMAPI_PORT` | `3001` | default — change on a port clash |
| `HERMES_MODEL_PRIMARY` | *(empty)* | default — blank auto-picks from the router's `/v1/models` |
| `HERMES_MODEL_FALLBACKS` | *(empty)* | default — comma-separated; the highest-value tuning available |
| `LINKEDIN_MCP_URL` | `http://linkedin-mcp:8000/mcp` | fixed — must match the compose `command:` flags |
| `LINKEDIN_VIEWER_PORT` | `6080` | default — noVNC login viewer |
| `HERMES_API_PORT` | `8080` | default |
| `HERMES_DASHBOARD_PORT` | `3000` | default |
| `HERMES_SANDBOX_IMAGE` | `hermes-linkedin/sandbox:latest` | default — produced by `make build` |
| `HERMES_SANDBOX_MEMORY_MB` | `1024` | default — below ~768 LibreOffice conversion starts failing; clamped to ≥128 |
| `HERMES_SANDBOX_CPUS` | `1.0` | default — clamped to ≥0.1 |
| `HERMES_SANDBOX_TIMEOUT_S` | `300` | default — clamped to ≥10 |
| `HERMES_SANDBOX_NETWORK` | `none` | default — `none` disables networking; leave it |
| `HERMES_SANDBOX_WORKSPACE` | `/data/workspaces` | fixed — container path, must stay under `HERMES_DATA_DIR` |
| `HERMES_SANDBOX_WORKSPACE_VOLUME` | `hermes_hermes-data` | **ignored** — see "Known wrinkles" |
| `HERMES_DATA_DIR` | `/data` | fixed — container path; never a Windows path |
| `HERMES_DOCKER_HOST` | `unix:///var/run/docker.sock` | fixed — effectively host root; point at a socket proxy to narrow it |
| `LOG_LEVEL` | `INFO` | default — `DEBUG` on `linkedin-mcp` is genuinely useful for empty scrapes |
| `HERMES_EXTRA_CORS_ORIGINS` | *(empty)* | optional — only for `vite dev` on :5173 or LAN access |

`.env` is git-ignored and excluded from `make ship`. Back up `ENCRYPTION_KEY` separately
(a password manager, not next to the backup): changing it makes the provider keys stored
inside `freellmapi-data` undecryptable.

### API surface

31 endpoints under `/api`, from nine route modules — full list at
http://127.0.0.1:8080/api/docs.

| Group | Endpoints |
|---|---|
| health | `GET /api/health` |
| settings | `GET` `PUT /api/settings` |
| llm | `GET /api/llm/models`, `POST /api/llm/test` |
| linkedin | `GET /api/linkedin/status`, `POST /api/linkedin/login` (returns instructions, not an automated login) |
| profile | `POST /api/profile/import`, `GET /api/profile` |
| resume | `POST /api/resume/upload`, `POST /api/resume/generate`, `POST /api/resume/score`, `GET /api/resumes`, `GET /api/resumes/{id}`, `GET /api/resumes/{id}/download` |
| jobs | `POST /api/jobs/search`, `GET /api/jobs`, `GET /api/jobs/{id}`, `PATCH /api/jobs/{id}`, `POST /api/jobs/{id}/tailor` |
| runs | `GET /api/runs`, `GET /api/runs/{id}`, `GET /api/runs/{id}/events` (SSE) |
| containers | `GET /api/containers`, `POST /api/containers/{id}/start\|stop\|restart`, `DELETE /api/containers/{id}`, `GET /api/containers/{id}/stats`, `GET /api/containers/{id}/logs` (SSE), `POST /api/sandbox/exec` |

---

## Everyday operations

**Logs**

```powershell
docker compose logs -f --tail=200                 # everything
docker compose logs -f --tail=200 hermes-core     # one service
docker compose logs freellmapi                    # e.g. the one-time setup code
```

```bash
make logs
make logs S=linkedin-mcp
```

**Restart one service** (a plain `restart` does not re-read `.env`; recreate instead)

```powershell
docker compose up -d --force-recreate hermes-core
docker compose restart linkedin-mcp     # fine when no env changed
```

**Rebuild after a code change**

```powershell
docker compose build hermes-core
docker compose up -d hermes-core
# sandbox image (profile-gated, so it needs the flag):
docker compose --profile build-only build hermes-sandbox-image
```

```bash
make build && make restart
```

Slim the sandbox image by ~500–800 MB, dropping only PDF output:

```bash
docker compose --profile build-only build --build-arg INCLUDE_PDF=0 hermes-sandbox-image
```

**Where resumes live.** Inside `hermes-core` at `/data/resumes` (rendered `.md`/`.docx`/
`.txt`/`.pdf`), uploads at `/data/uploads`, per-run sandbox workspaces at
`/data/workspaces`, and the SQLite DB at `/data/hermes.db` — all on the
`hermes-linkedin_hermes-data` volume. Normal retrieval is the dashboard's download
button (`GET /api/resumes/{id}/download?fmt=docx`). To pull files out directly:

```powershell
docker cp hermes-core:/data/resumes .\resumes-out
```

**Back up your data** (DB + provider keys; the LinkedIn session is deliberately excluded)

```powershell
.\scripts\backup.ps1                # stops the stack first for a clean SQLite snapshot
.\scripts\backup.ps1 -Live          # faster, may capture the DB mid-write — not for a migration
```

**Reset state**

```bash
make reset          # DESTRUCTIVE: containers + all three volumes. Prompts for YES.
make down           # containers only; data kept
```

**Re-login when the LinkedIn session expires.** LinkedIn sessions expire, and LinkedIn
may invalidate one after unusual activity; `/api/linkedin/status` flips to
`authenticated: false` and the dashboard shows "LinkedIn not connected". Re-running the
login is the normal, expected fix — about a minute:

```powershell
.\scripts\linkedin-login.ps1
```

---

## Shipping to another laptop

Two paths, and it is a real tradeoff. Neither carries your `.env`, your database, or your
LinkedIn session.

### A. Image bundle — offline-capable, big

Roughly 3–6 GB and 5–15 minutes to write; `docker load` on arrival is 2–5 minutes. Pick
this for an air-gapped or metered machine, or when you want byte-identical images to the
ones you tested.

```powershell
# old machine
.\scripts\ship.ps1                      # -> dist\hermes-images.tar + dist\repo\
# new machine, from inside the unpacked repo
.\scripts\load.ps1                      # docker load, verify 5 images, .env, up -d --no-build
```

```bash
make ship        # -> dist/hermes-images.tar + dist/hermes-repo.tar.gz
# target machine:
tar xzf hermes-repo.tar.gz && make load && make bootstrap && make up
```

### B. Source rebuild — small, needs internet

Roughly 1–5 MB. The target rebuilds from the Dockerfiles, so it needs internet for base
images and packages and 5–15 minutes of build time. Rebuilt images are not guaranteed
byte-identical, since upstream bases and package versions move.

```powershell
.\scripts\ship.ps1 -Mode source -Zip
# new machine:
powershell -ExecutionPolicy Bypass -File .\scripts\bootstrap.ps1
```

### On both paths, on the new machine

1. **Re-mint or re-enter `FREELLMAPI_KEY`.** `.env` is never shipped.
2. **Move your data separately if you want it:** `scripts/backup.ps1` on the old machine
   → `make restore` (stack down) on the new one. Record `ENCRYPTION_KEY` too, or the
   restored provider keys cannot be decrypted.
3. **Log into LinkedIn again on that machine** — one minute, once.

> ### Do NOT copy the `linkedin-session` volume between machines
>
> `ship` excludes it, `backup` refuses it, and `-Force` does not override that. It is a
> live authenticated browser profile — session cookies, auth tokens, and a device
> fingerprint. Copying it is:
>
> - **fragile** — the fingerprint no longer matches the machine replaying it, so LinkedIn
>   is *more* likely to invalidate the session outright, or flag the account; and
> - **a security risk** — those files are bearer credentials to your LinkedIn account, in
>   plain form, in every copy of the backup you keep.
>
> The supported way to have a session on a machine is to log in on that machine.

---

## Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| `docker: command not found`, or `Docker engine is not responding` | Docker Desktop not installed or not started | Start Docker Desktop, wait for the whale to settle, re-run. `docker info --format '{{.ServerVersion}}'` must print a version |
| Containers page empty; `POST /api/sandbox/exec` and resume rendering fail; `/api/health` shows `docker: false` | The Docker socket is not reachable from `hermes-core` | Confirm the `- /var/run/docker.sock:/var/run/docker.sock` mount is present, and that Docker Desktop exposes the socket. On Linux the socket's group must permit the daemon connection. Mounting it `:ro` deliberately disables sandbox spawning and the start/stop buttons |
| `Docker Compose v2 is missing` | Legacy `docker-compose` only | Hermes needs the `docker compose` subcommand. Upgrade Docker |
| Every LLM feature fails; 401 from the router; `/api/health` says `llm_key_present: false` | `FREELLMAPI_KEY` blank or not yet applied | Mint the token at http://127.0.0.1:3001, paste it into `.env` **with** the `freellmapi-` prefix, then `docker compose up -d --force-recreate hermes-core`. A plain `restart` will not pick it up |
| Router dashboard has no models | No provider key added yet | Add at least one free provider key in the freellmapi dashboard. From another device, get the one-time setup code from `docker compose logs freellmapi` |
| "LinkedIn not connected"; `No valid LinkedIn session is available in Docker...` | The `linkedin-session` volume has no valid session, or it expired | Run `.\scripts\linkedin-login.ps1` (or `make login-linkedin`), sign in by hand at http://127.0.0.1:6080/vnc.html, reach your feed, then verify `/api/linkedin/status` shows `authenticated: true` |
| Login container fails with a browser-profile lock error | `linkedin-mcp` is holding the Chromium profile | Let the script stop it for you (do not pass `-KeepRunning`), or `docker compose stop linkedin-mcp` first |
| Leftover login container holds port 6080 | Previous one-shot not removed | `docker compose --profile login rm -f linkedin-login` |
| `port is already allocated` on 3001 (or 3000/8080/6080) | Another stack owns it. **A separate `hermes-agent` project on this machine also publishes freellmapi on 3001** and owns `hermes_*` volumes | Run one stack at a time, or change `FREELLMAPI_PORT` / `HERMES_DASHBOARD_PORT` / `HERMES_API_PORT` / `LINKEDIN_VIEWER_PORT` in `.env` and `make restart`. Distinct project names (`hermes-linkedin` vs `hermes`) already keep the two from adopting each other's volumes — only host ports contend |
| PDF download 404s with an explanation; `.docx` and `.txt` are fine | LibreOffice is absent from the sandbox image (built with `INCLUDE_PDF=0`, or the apt step failed) | Expected and non-fatal — `md`/`docx`/`txt` still generate. To get PDFs back: `docker compose --profile build-only build hermes-sandbox-image` (no build arg). With LibreOffice the image is ~700 MB–1 GB; without, ~180 MB |
| PDF conversion fails or the container is killed mid-render | Sandbox memory cap too low | Raise `HERMES_SANDBOX_MEMORY_MB` (LibreOffice starts failing below ~768) or `HERMES_SANDBOX_TIMEOUT_S`, then `make restart` |
| Runs fail intermittently with 429s; output quality swings between runs | Free tiers rate-limit aggressively and models differ a lot | List two or three `HERMES_MODEL_FALLBACKS` from *different* upstream providers, and add more provider keys so the router can fail over. Pin `HERMES_MODEL_PRIMARY` once you know which free model your mix serves reliably |
| Windows: `-v $PWD/dist:/backup` style mounts resolve to nothing; paths look mangled | Git Bash rewrites POSIX-looking paths before Docker sees them | Use the provided scripts — they stream tars through stdout/stdin and normalise `/c/x` → `C:/x` instead of bind-mounting host directories |
| Windows: `make` not found | Docker Desktop does not ship `make` | Use `.\scripts\*.ps1`, or run targets from Git Bash, or `choco install make` |
| Windows: `.ps1` blocked by execution policy | Default policy | `powershell -ExecutionPolicy Bypass -File .\scripts\bootstrap.ps1` |
| Run log / container log stops updating, or arrives only at the end | Something is buffering the SSE stream | `hermes-dashboard`'s nginx is configured for this (`proxy_buffering off`, empty `Connection` header, 3600s read timeout, no gzip on `/api`). If you bypassed it — hitting `:8080` through another proxy, or a corporate TLS middlebox — put the dashboard back in front, or check `/api/runs/{id}` which returns the same events as a plain JSON payload |
| Scrapes return empty sections | LinkedIn markup changed, or the page did not finish loading | `LOG_LEVEL=DEBUG` and `make restart`, then read `docker compose logs -f linkedin-mcp`. `TOOL_TIMEOUT` is already 180s |

---

## Limits & risks

Read this part. It is not boilerplate.

- **Running an automated client against LinkedIn can violate the LinkedIn User Agreement,
  and carries a real, non-theoretical risk that your account is restricted or banned.**
  The `linkedin-mcp` container drives a real logged-in browser using **your** cookies;
  from LinkedIn's side that traffic is attributable to you and nobody else. Consequences
  range from a temporary read-only restriction, through forced identity verification, to
  permanent termination — and recovering a restricted account is slow and often
  unsuccessful. Risk scales with volume: large `max_pages`, tight scheduling and long
  unbroken sessions are the patterns that get flagged. A secondary account is not a clean
  workaround (LinkedIn's terms also prohibit multiple accounts, and you may lose that one
  too). The safest configuration of Hermes is one where you paste your profile and target
  job descriptions in by hand and never start `linkedin-mcp` at all — the resume builder
  and the ATS scorer work fine that way.
- **Hermes never auto-submits an application.** No apply tool exists in the stack. It does
  not fill forms, click Submit, send Easy Apply requests, or message recruiters. If you
  were hoping for a bot that applies to 400 jobs overnight, this is not it and will not
  become it.
- **The ATS score is a heuristic proxy, not a vendor score.** It is a deterministic
  weighted sum of documented subscores — parseability 20, keyword coverage 25, contact
  block 10, experience quality 20, formatting 15, readability 10 — built from published
  ATS parsing guidance, plus an optional LLM semantic pass. Workday, Greenhouse, Taleo,
  iCIMS and Lever each parse differently and none of them publish a score. A high Hermes
  score means "clean, parseable, on-topic", which is worth a lot. It is not a prediction
  about any employer's pipeline, and no score guarantees an interview.
- **Free-tier models vary in quality and rate-limit hard.** Output quality differs between
  providers and between runs; 429s are routine. Multiple provider keys and a fallback
  chain are how you make this usable, not an optimisation.
- **Your resume, profile text and scraped job descriptions are sent to whichever upstream
  model you configure**, and free tiers commonly reserve the right to retain or train on
  that content. Check each provider's policy. If that is unacceptable, point the router at
  a host-local Ollama or LM Studio via `host.docker.internal` — Hermes never needs to know
  the difference.
- **`/var/run/docker.sock` is mounted into `hermes-core`, which is effectively root on the
  host.** That is what makes container sandbox mode and the Containers page work. A
  remote-code-execution bug in `hermes-core` would therefore be a host compromise, not a
  container compromise. This is an accepted trade-off for a single-user, loopback-bound,
  self-hosted tool, and is **not** acceptable for a multi-tenant or internet-exposed
  deployment. Mitigations, in order of effort: keep `HOST_BIND=127.0.0.1`; mount the
  socket `:ro` (read-only views keep working, sandbox spawning and the start/stop buttons
  stop); front it with a socket proxy and point `HERMES_DOCKER_HOST` there; or drop the
  mount and accept that rendering, `/api/sandbox/exec` and the Containers page fail loudly.
- **There is no authentication in front of any Hermes service.** Every port binds to
  `127.0.0.1` by default. Setting `HOST_BIND=0.0.0.0` publishes your LinkedIn session,
  your resume, and an LLM router holding provider keys to everyone who can reach that
  interface.
- **`make backup` and `scripts/backup.*` produce files containing your data and your
  encrypted provider keys.** Treat them like a password database. (Neither contains the
  LinkedIn session — that is refused on purpose.)
- **Nothing here is legal advice, and there is no warranty.** Hermes' authors and the
  authors of the third-party images make no representation that this software's use
  complies with LinkedIn's terms, and accept no liability for any action LinkedIn takes
  against your account.

### Known wrinkles in this checkout

- `HERMES_SANDBOX_WORKSPACE_VOLUME` in `.env` / `.env.example` is **dead configuration**.
  Nothing reads it: `hermes-core` inspects its own container's mount table and rewrites the
  workspace path to the daemon-side source automatically. Its value (`hermes_hermes-data`)
  and the comment above it are also stale — the compose project name is `hermes-linkedin`,
  so the real volume is `hermes-linkedin_hermes-data`.
- The `Makefile` hardcodes `PROJECT := hermes`, which does not match the compose file's
  `name: hermes-linkedin`. `make backup` and `make restore` therefore address
  `hermes_hermes-data` etc. and will silently create/read empty volumes rather than your
  real ones. **Use `scripts/backup.ps1` / `scripts/backup.sh` instead** — those resolve
  volume names against the live Docker daemon. `make reset` is unaffected in behaviour
  (it uses `down -v`); only the volume names it echoes are wrong.

---

## Further reading

- [LICENSE-NOTICE.md](LICENSE-NOTICE.md) — third-party components, the LinkedIn
  terms-of-service warning in full, why there is no apply tool, and operational security
  notes.
- `docs/ARCHITECTURE.md`, `docs/RUNBOOK.md` — referenced by the bootstrap, load and ship
  scripts.
#   h e r m e s - l i n k e d - a p p l i e r  
 