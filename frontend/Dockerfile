# syntax=docker/dockerfile:1
#
# NUVEXA AutoTask dashboard image.
#   Stage 1: build the static bundle with Node (npm ci against the committed lockfile).
#   Stage 2: serve it with nginx-unprivileged (runs as uid 101, listens on 8080) and reverse-proxy
#            /api/ to the FastAPI service so the session cookie stays same-origin.
#
#   docker build -t nuvexa-frontend frontend
#   docker run -p 8080:8080 -e API_UPSTREAM=http://host.docker.internal:8000 nuvexa-frontend

# ---------------------------------------------------------------------------------------------
# Base images are pinned by tag and digest; Dependabot (.github/dependabot.yml) updates both.
FROM node:24.22.1-alpine@sha256:06d7adf3e0ef61a13f1d28682726366f97a15aa1888c934d3ecec86c33918b53 AS build
WORKDIR /app
ENV CI=true \
    NPM_CONFIG_UPDATE_NOTIFIER=false \
    NPM_CONFIG_FUND=false \
    NPM_CONFIG_AUDIT=false

# .npmrc: ignore-scripts (no install-time code execution) and min-release-age (cooldown).
COPY package.json package-lock.json .npmrc ./
RUN npm ci

COPY . .
RUN npm run build

# ---------------------------------------------------------------------------------------------
FROM nginxinc/nginx-unprivileged:1.31.7-alpine@sha256:b5047b9b76845eac124358373c6852377c52e4b05834a47647e955dd91cb8f1e AS runtime

LABEL org.opencontainers.image.title="nuvexa-autotask-dashboard" \
      org.opencontainers.image.description="NUVEXA AutoTask (by Elarxsys) web dashboard" \
      org.opencontainers.image.vendor="Elarxsys"

# API_UPSTREAM, NGINX_REAL_IP_FROM (trusted proxy CIDR) and the DNS resolvers from /etc/resolv.conf are
# rendered into the server config at
# container start by the image's stock entrypoint. The filter restricts envsubst to exactly these
# two variables, so nginx runtime variables ($host, $uri, …) are never touched.
ENV API_UPSTREAM=http://api:8000 \
    NGINX_REAL_IP_FROM=127.0.0.1/32 \
    NGINX_ENTRYPOINT_LOCAL_RESOLVERS=1 \
    NGINX_ENVSUBST_FILTER=^(API_UPSTREAM|NGINX_LOCAL_RESOLVERS|NGINX_REAL_IP_FROM)$ \
    NGINX_ENTRYPOINT_QUIET_LOGS=1

# Rendered to /etc/nginx/conf.d/default.conf (replacing the stock server) on every start.
# Read-only root filesystem: mount a writable tmpfs/emptyDir at /etc/nginx/conf.d and /tmp.
COPY --chown=nginx:root nginx.conf /etc/nginx/templates/default.conf.template

USER root
# Security: pick up Alpine security fixes published after the base image was built (Trivy gate in CI).
RUN apk upgrade --no-cache \
 && rm -rf /usr/share/nginx/html/*
COPY --from=build --chown=nginx:root /app/dist /usr/share/nginx/html
USER nginx

EXPOSE 8080
HEALTHCHECK --interval=30s --timeout=3s --start-period=5s --retries=3 \
    CMD wget -q -O /dev/null http://127.0.0.1:8080/healthz || exit 1
