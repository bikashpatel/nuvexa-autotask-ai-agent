# syntax=docker/dockerfile:1
#
# NUVEXA AutoTask dashboard image.
#   Stage 1: build the static bundle with Node (npm ci against the committed lockfile).
#   Stage 2: serve it with nginx-unprivileged (runs as uid 101, listens on 8080) and reverse-proxy
#            /api/ to the FastAPI service so the session cookie stays same-origin.
#
#   docker build -t nuvexa-frontend frontend
#   docker run -p 8080:8080 -e API_UPSTREAM=http://host.docker.internal:8000 nuvexa-frontend

ARG NODE_VERSION=24.22.1
ARG NGINX_VERSION=1.30.6

# ---------------------------------------------------------------------------------------------
FROM node:${NODE_VERSION}-alpine AS build
WORKDIR /app
ENV CI=true \
    NPM_CONFIG_UPDATE_NOTIFIER=false \
    NPM_CONFIG_FUND=false \
    NPM_CONFIG_AUDIT=false

COPY package.json package-lock.json ./
RUN npm ci

COPY . .
RUN npm run build

# ---------------------------------------------------------------------------------------------
FROM nginxinc/nginx-unprivileged:${NGINX_VERSION}-alpine AS runtime

LABEL org.opencontainers.image.title="nuvexa-autotask-dashboard" \
      org.opencontainers.image.description="NUVEXA AutoTask (by Elarxsys) web dashboard" \
      org.opencontainers.image.vendor="Elarxsys"

# API_UPSTREAM (and the DNS resolvers from /etc/resolv.conf) are rendered into the server config at
# container start by the image's stock entrypoint. The filter restricts envsubst to exactly these
# two variables, so nginx runtime variables ($host, $uri, …) are never touched.
ENV API_UPSTREAM=http://api:8000 \
    NGINX_ENTRYPOINT_LOCAL_RESOLVERS=1 \
    NGINX_ENVSUBST_FILTER=^(API_UPSTREAM|NGINX_LOCAL_RESOLVERS)$ \
    NGINX_ENTRYPOINT_QUIET_LOGS=1

# Rendered to /etc/nginx/conf.d/default.conf (replacing the stock server) on every start.
# Read-only root filesystem: mount a writable tmpfs/emptyDir at /etc/nginx/conf.d and /tmp.
COPY --chown=nginx:root nginx.conf /etc/nginx/templates/default.conf.template

USER root
RUN rm -rf /usr/share/nginx/html/*
COPY --from=build --chown=nginx:root /app/dist /usr/share/nginx/html
USER nginx

EXPOSE 8080
HEALTHCHECK --interval=30s --timeout=3s --start-period=5s --retries=3 \
    CMD wget -q -O /dev/null http://127.0.0.1:8080/healthz || exit 1
