# syntax=docker/dockerfile:1
###############################################################################
# Stage 1 - build the Vite bundle
###############################################################################
FROM node:20-alpine AS build

WORKDIR /app

ENV CI=true \
    NODE_ENV=development

# Dependency layer first so edits to src/ don't reinstall node_modules.
#
# NOTE: this repo intentionally ships WITHOUT a package-lock.json, so we use
#       `npm install`. Once a lockfile is committed, switch the two lines below to:
#           COPY package.json package-lock.json ./
#           RUN npm ci --no-audit --no-fund
#       `npm ci` is reproducible and faster, but it hard-fails without a lockfile.
COPY package.json ./
RUN npm install --no-audit --no-fund

# App sources + build config (.dockerignore keeps node_modules/dist out).
COPY tsconfig.json tsconfig.node.json vite.config.ts tailwind.config.js postcss.config.js index.html ./
COPY src ./src

# `npm run build` is `vite build` only - deliberately not gated on `tsc`, so a
# type-only nit can never brick an image build. Run `npm run typecheck` in CI.
RUN npm run build

###############################################################################
# Stage 2 - serve the static bundle with nginx
###############################################################################
FROM nginx:alpine AS runtime

# Replace the stock site with ours (SPA fallback + SSE-safe /api proxy).
RUN rm -f /etc/nginx/conf.d/default.conf
COPY nginx.conf /etc/nginx/conf.d/hermes.conf

COPY --from=build /app/dist /usr/share/nginx/html

# Fail fast on a malformed config at build time rather than at container start.
RUN nginx -t

EXPOSE 80

HEALTHCHECK --interval=30s --timeout=5s --start-period=5s --retries=3 \
  CMD wget -q -O /dev/null http://127.0.0.1/healthz || exit 1

STOPSIGNAL SIGQUIT

CMD ["nginx", "-g", "daemon off;"]
