# =============================================================================
#  Hermes — operator Makefile
# =============================================================================
#  Every recipe is plain POSIX sh (no bashisms) so it behaves the same under
#  Linux, macOS and Git Bash on Windows.
#
#  WINDOWS USERS: `make` is not installed with Docker Desktop. Your options,
#  best first:
#     1. run the PowerShell equivalents in .\scripts\*.ps1
#        There is no up.ps1/down.ps1: use `docker compose up -d` / `down`
#        directly, and the real scripts for the rest:
#        .\scripts\bootstrap.ps1, linkedin-login.ps1, ship.ps1, load.ps1,
#        backup.ps1, restore.ps1
#     2. run these targets from Git Bash, which ships a usable sh
#        (`make` itself still has to be installed, e.g. via `choco install make`)
#     3. read the recipe below and run the `docker compose ...` line by hand —
#        every target here is a thin wrapper, nothing is hidden.
#
#  Ports are duplicated here as overridable variables rather than parsed out of
#  .env, because sourcing .env portably is fragile. If you changed a port in
#  .env, pass it through:   make health API_PORT=9090
# =============================================================================

SHELL := /bin/sh
.DEFAULT_GOAL := help

# MUST match `name:` in docker-compose.yml — Docker prefixes every named volume
# with it. When these drift, `backup` mounts volumes that do not exist, Docker
# silently creates them EMPTY, and you get a valid-looking archive with nothing
# in it. The `require-volumes` guard below exists so that can never pass silently
# again.
PROJECT       := hermes-linkedin
DC            := docker compose
DIST          := dist

# Host ports — keep in sync with .env if you change them there.
API_PORT       ?= 8080
DASHBOARD_PORT ?= 3000
LLM_PORT       ?= 3001
VIEWER_PORT    ?= 6080

# Optional service filter for `logs` / `ps`:   make logs S=hermes-core
S ?=

# The five images that make up a complete, offline-installable Hermes: three
# built locally (core, dashboard, sandbox) and two pulled from a registry.
# Keep this list in sync with the `image:` keys in docker-compose.yml.
IMAGES := \
	hermes-core:latest \
	hermes-dashboard:latest \
	hermes-linkedin/sandbox:latest \
	ghcr.io/tashfeenahmed/freellmapi:latest \
	stickerdaniel/linkedin-mcp-server:4.23.2

# Docker prefixes named volumes with the compose project name, which
# docker-compose.yml pins to `hermes-linkedin`.
VOLUMES := \
	$(PROJECT)_hermes-data \
	$(PROJECT)_freellmapi-data \
	$(PROJECT)_linkedin-session

# `--profile build-only` is required for the sandbox image to be built at all;
# `--profile login` is required for `down`/`reset` to also clean up the one-shot
# login container if it was left behind.
DC_ALL := $(DC) --profile build-only --profile login

.PHONY: help bootstrap build up down restart logs ps login-linkedin \
        shell-core health reset ship load backup restore clean

# -----------------------------------------------------------------------------
help: ## Show this help (default target)
	@printf '\nHermes — self-hosted LinkedIn job scout + ATS resume builder\n\n'
	@printf 'Usage: make <target> [VAR=value]\n\n'
	@awk 'BEGIN {FS = ":.*?## "} /^[a-zA-Z_-]+:.*?## / {printf "  %-16s %s\n", $$1, $$2}' $(MAKEFILE_LIST)
	@printf '\nFirst run:\n'
	@printf '  1) make bootstrap      # create .env, generate ENCRYPTION_KEY\n'
	@printf '  2) make build          # build core, dashboard and the sandbox image\n'
	@printf '  3) make up             # start the stack\n'
	@printf '  4) open http://127.0.0.1:$(LLM_PORT)  -> add provider keys, mint a\n'
	@printf '     freellmapi-... token, paste it into .env as FREELLMAPI_KEY\n'
	@printf '  5) make restart        # pick up the new token\n'
	@printf '  6) make login-linkedin # log into LinkedIn by hand in the viewer\n'
	@printf '  7) open http://127.0.0.1:$(DASHBOARD_PORT)\n\n'

# -----------------------------------------------------------------------------
bootstrap: ## Create .env from .env.example and generate ENCRYPTION_KEY
	@if [ -f .env ]; then \
		echo "[bootstrap] .env already exists - leaving it untouched."; \
	else \
		cp .env.example .env; \
		echo "[bootstrap] created .env from .env.example"; \
		KEY=`openssl rand -hex 32 2>/dev/null \
			|| python -c 'import secrets;print(secrets.token_hex(32))' 2>/dev/null \
			|| python3 -c 'import secrets;print(secrets.token_hex(32))'`; \
		if [ -z "$$KEY" ]; then \
			echo "[bootstrap] ERROR: could not generate a key (no openssl, no python)."; \
			echo "[bootstrap] Put 64 hex chars in ENCRYPTION_KEY in .env by hand."; \
			exit 1; \
		fi; \
		sed -i.bak "s|^ENCRYPTION_KEY=.*|ENCRYPTION_KEY=$$KEY|" .env; \
		rm -f .env.bak; \
		echo "[bootstrap] generated ENCRYPTION_KEY (64 hex chars)"; \
	fi
	@mkdir -p $(DIST)
	@echo ""
	@echo "[bootstrap] Remaining MANUAL step: FREELLMAPI_KEY in .env."
	@echo "[bootstrap] It can only be minted from the router's own dashboard,"
	@echo "[bootstrap] so run 'make build && make up', open"
	@echo "[bootstrap] http://127.0.0.1:$(LLM_PORT), add a free provider key, create a"
	@echo "[bootstrap] client token (freellmapi-...), paste it into .env, 'make restart'."
	@echo ""

# -----------------------------------------------------------------------------
build: ## Pull third-party images and build core, dashboard and sandbox images
	@echo "[build] pulling pinned third-party images..."
	@# --ignore-buildable skips services that have a `build:` key. It landed in
	@# Compose v2.20; the fallback covers older CLIs, where pulling the
	@# not-yet-built first-party images is expected to fail harmlessly.
	@$(DC_ALL) pull --ignore-buildable 2>/dev/null \
		|| $(DC_ALL) pull --ignore-pull-failures
	@echo "[build] building first-party images (includes the build-only sandbox image)..."
	$(DC_ALL) build
	@echo "[build] done. Sandbox image tagged as hermes-linkedin/sandbox:latest (see HERMES_SANDBOX_IMAGE)."

# -----------------------------------------------------------------------------
up: ## Start the stack in the background
	@if [ ! -f .env ]; then echo "[up] no .env - run 'make bootstrap' first."; exit 1; fi
	$(DC) up -d
	@echo ""
	@echo "  dashboard  http://127.0.0.1:$(DASHBOARD_PORT)"
	@echo "  api        http://127.0.0.1:$(API_PORT)/api/health"
	@echo "  llm router http://127.0.0.1:$(LLM_PORT)"
	@echo ""
	@echo "  'make health' to verify, 'make logs' to follow."

# -----------------------------------------------------------------------------
down: ## Stop and remove containers (volumes and your data are KEPT)
	$(DC_ALL) down --remove-orphans

# -----------------------------------------------------------------------------
restart: ## Recreate containers so .env changes take effect
	@# A bare `docker compose restart` does NOT re-read .env: environment is
	@# fixed at container-create time. Recreating is the only correct way to
	@# apply a new FREELLMAPI_KEY or model setting.
	$(DC) up -d --force-recreate
	@echo "[restart] containers recreated with the current .env."

# -----------------------------------------------------------------------------
logs: ## Follow logs; one service with:  make logs S=hermes-core
	$(DC_ALL) logs -f --tail=200 $(S)

# -----------------------------------------------------------------------------
ps: ## Show container status
	$(DC_ALL) ps $(S)

# -----------------------------------------------------------------------------
login-linkedin: ## Interactive LinkedIn login via the noVNC viewer (one-shot)
	@echo ""
	@echo "  Starting the login container. When it is up, open:"
	@echo ""
	@echo "      http://127.0.0.1:$(VIEWER_PORT)/vnc.html"
	@echo ""
	@echo "  Log in AS YOURSELF in that browser (Hermes never types or stores"
	@echo "  your LinkedIn credentials). Complete any 2FA / checkpoint. The"
	@echo "  session is written to the shared 'linkedin-session' volume."
	@echo "  Press Ctrl-C here when you are done, then run 'make restart'."
	@echo ""
	@# `up` (not `run`) because the service declares container_name, and the
	@# interaction happens in the browser rather than on this console. If the
	@# entrypoint ever needs console input: docker attach hermes-linkedin-login
	@# linkedin-mcp holds an exclusive lock on the Chromium profile inside the
	@# shared linkedin-session volume, so it must be stopped first or the login
	@# browser fails to open the profile. scripts/linkedin-login.* do the same.
	-$(DC) stop linkedin-mcp
	$(DC) --profile login up --abort-on-container-exit linkedin-login
	-$(DC) start linkedin-mcp
	@echo "[login] container exited and linkedin-mcp restarted with the new session."

# -----------------------------------------------------------------------------
shell-core: ## Open a shell inside the hermes-core container
	@$(DC) exec hermes-core /bin/bash 2>/dev/null || $(DC) exec hermes-core /bin/sh

# -----------------------------------------------------------------------------
health: ## Probe every service's health endpoint
	@echo "--- hermes-core /api/health ---"
	@curl -fsS "http://127.0.0.1:$(API_PORT)/api/health" \
		|| echo "  UNREACHABLE (is the stack up? 'make ps')"
	@echo ""
	@echo "--- freellmapi /api/ping ---"
	@curl -fsS "http://127.0.0.1:$(LLM_PORT)/api/ping" \
		|| echo "  UNREACHABLE"
	@echo ""
	@echo "--- dashboard ---"
	@curl -fsS -o /dev/null -w "  HTTP %{http_code}\n" "http://127.0.0.1:$(DASHBOARD_PORT)/" \
		|| echo "  UNREACHABLE"
	@echo ""
	@echo "--- linkedin session (via hermes-core) ---"
	@curl -fsS "http://127.0.0.1:$(API_PORT)/api/linkedin/status" \
		|| echo "  UNREACHABLE"
	@echo ""

# -----------------------------------------------------------------------------
reset: ## DESTRUCTIVE - delete containers AND all volumes (DB, resumes, session)
	@echo "This will permanently delete:"
	@echo "  - $(PROJECT)_hermes-data        (SQLite DB, generated resumes, workspaces)"
	@echo "  - $(PROJECT)_freellmapi-data    (your stored LLM provider keys)"
	@echo "  - $(PROJECT)_linkedin-session   (your LinkedIn login; you must log in again)"
	@printf 'Type YES to continue: '
	@read ans; [ "$$ans" = "YES" ] || { echo "aborted."; exit 1; }
	$(DC_ALL) down -v --remove-orphans
	@echo "[reset] gone. 'make up' starts from a clean slate."

# -----------------------------------------------------------------------------
ship: ## Package images + source into ./dist for offline transfer
	@mkdir -p $(DIST)
	@echo "[ship] saving images -> $(DIST)/hermes-images.tar"
	@echo "[ship] (this is a few GB and takes a while; Playwright/Chrome is large)"
	docker save -o $(DIST)/hermes-images.tar $(IMAGES)
	@echo "[ship] archiving source -> $(DIST)/hermes-repo.tar.gz"
	@# .env is EXCLUDED on purpose: it holds your ENCRYPTION_KEY and router
	@# token. Volumes are excluded too - use `make backup` for those, and move
	@# that archive separately and deliberately.
	tar -czf $(DIST)/hermes-repo.tar.gz \
		--exclude='./.env' \
		--exclude='./.git' \
		--exclude='./$(DIST)' \
		--exclude='./data' \
		--exclude='./artifacts' \
		--exclude='*/node_modules' \
		--exclude='*/__pycache__' \
		--exclude='*.db' \
		--exclude='*.db-wal' \
		--exclude='*.db-shm' \
		-C . .
	@echo ""
	@echo "[ship] $(DIST)/ contains:"
	@ls -lh $(DIST)
	@echo ""
	@echo "[ship] On the target machine:"
	@echo "         tar xzf hermes-repo.tar.gz && make load && make bootstrap && make up"

# -----------------------------------------------------------------------------
load: ## Load images from ./dist/hermes-images.tar (the other half of `ship`)
	@if [ ! -f $(DIST)/hermes-images.tar ]; then \
		echo "[load] $(DIST)/hermes-images.tar not found."; exit 1; \
	fi
	docker load -i $(DIST)/hermes-images.tar
	@echo "[load] images loaded. 'make bootstrap' then 'make up' (no build needed)."

# -----------------------------------------------------------------------------
.PHONY: require-volumes
require-volumes: ## Verify the named data volumes actually exist
	@missing=''; 	for v in $(VOLUMES); do 		docker volume inspect "$$v" >/dev/null 2>&1 || missing="$$missing $$v"; 	done; 	if [ -n "$$missing" ]; then 		echo "[error] These Docker volumes do not exist:$$missing"; 		echo "[error] PROJECT is '$(PROJECT)'; it must match \`name:\` in docker-compose.yml."; 		echo "[error] Volumes currently on this machine:"; 		docker volume ls --format '  {{.Name}}' | grep -i hermes || echo "  (none)"; 		echo "[error] Refusing to continue: a read-only mount of a missing volume"; 		echo "[error] makes Docker create it EMPTY, which would produce an empty backup."; 		exit 1; 	fi
	@echo "[ok] all $(words $(VOLUMES)) data volumes present"

# -----------------------------------------------------------------------------
backup: require-volumes ## Archive all three data volumes to ./dist/hermes-volumes.tar.gz
	@mkdir -p $(DIST)
	@echo "[backup] archiving $(VOLUMES)"
	@# Deliberately streams the tar to stdout instead of bind-mounting a host
	@# directory: Git Bash on Windows rewrites POSIX-looking paths before Docker
	@# ever sees them, which breaks `-v $$PWD/dist:/backup`. Redirection has no
	@# such problem.
	docker run --rm \
		-v $(PROJECT)_hermes-data:/v/hermes-data:ro \
		-v $(PROJECT)_freellmapi-data:/v/freellmapi-data:ro \
		-v $(PROJECT)_linkedin-session:/v/linkedin-session:ro \
		alpine:3.20 tar -czf - -C /v . > $(DIST)/hermes-volumes.tar.gz
	@ls -lh $(DIST)/hermes-volumes.tar.gz
	@echo "[backup] NOTE: this archive contains your LinkedIn session cookies and"
	@echo "[backup] your encrypted provider keys. Treat it like a password file."

# -----------------------------------------------------------------------------
restore: ## Restore volumes from ./dist/hermes-volumes.tar.gz (stack must be down)
	@if [ ! -f $(DIST)/hermes-volumes.tar.gz ]; then \
		echo "[restore] $(DIST)/hermes-volumes.tar.gz not found."; exit 1; \
	fi
	@printf 'This overwrites the contents of the Hermes volumes. Type YES to continue: '
	@read ans; [ "$$ans" = "YES" ] || { echo "aborted."; exit 1; }
	$(DC_ALL) down --remove-orphans
	docker run --rm -i \
		-v $(PROJECT)_hermes-data:/v/hermes-data \
		-v $(PROJECT)_freellmapi-data:/v/freellmapi-data \
		-v $(PROJECT)_linkedin-session:/v/linkedin-session \
		alpine:3.20 tar -xzf - -C /v < $(DIST)/hermes-volumes.tar.gz
	@echo "[restore] done. 'make up' to start with the restored data."

# -----------------------------------------------------------------------------
clean: ## Remove ./dist and prune dangling images (keeps volumes)
	rm -rf $(DIST)
	-$(DC_ALL) down --remove-orphans
	-docker image prune -f
	@echo "[clean] build artifacts removed. Volumes untouched - use 'make reset' for those."
