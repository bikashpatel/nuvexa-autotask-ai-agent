# syntax=docker/dockerfile:1
#
# Backend image for NUVEXA-AutoTask-AI-Agent (Elarxsys).
#
# One image, three process types (see docker-compose.yml / docs/deployment.md):
#   nuvexa api --host 0.0.0.0 --port 8000   (default CMD)
#   nuvexa worker                           (Postgres job queue consumer, no HTTP port)
#   nuvexa migrate                          (alembic upgrade head, one-shot)
#
# No secrets are baked into the image; all configuration comes from environment variables
# (see .env.example). Build: docker build -t nuvexa-backend:local .
#
# Pattern follows https://docs.astral.sh/uv/guides/integration/docker/ (multi-stage, cache mounts,
# dependencies installed in a separate layer from the project).

# Base images are pinned by tag *and* digest (Dependabot updates both; the digest makes builds reproducible).
# uv is pinned to the version that produced uv.lock. Bump both together.
FROM ghcr.io/astral-sh/uv:0.13.6@sha256:af22a52f5eafef3d78e551599d405c02df61919d18028f0cea8eda93bc9986d0 AS uv

# ---------------------------------------------------------------------------------------------
# Builder: resolve and install the locked, non-dev dependency set into /app/.venv
# ---------------------------------------------------------------------------------------------
FROM python:3.14-slim-trixie@sha256:35caba83af2fe6da9a5d922f9b0156f21d1246a48278c58a08bd253303ba42e8 AS builder

COPY --from=uv /uv /uvx /bin/

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=0 \
    UV_PROJECT_ENVIRONMENT=/app/.venv

WORKDIR /app

# 1) Dependencies only (cached until pyproject.toml / uv.lock change).
RUN --mount=type=cache,target=/root/.cache/uv \
    --mount=type=bind,source=uv.lock,target=uv.lock \
    --mount=type=bind,source=pyproject.toml,target=pyproject.toml \
    uv sync --locked --no-install-project --no-dev

# 2) Project source. The project is installed in editable mode at /app so that `nuvexa migrate`
#    finds alembic.ini and app/db/migrations in the working tree at the same path at runtime.
COPY pyproject.toml uv.lock README.md alembic.ini ./
COPY app ./app
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --locked --no-dev \
 && /app/.venv/bin/python -m compileall -q /app/app

# ---------------------------------------------------------------------------------------------
# Runtime: same Python base, no uv, no build tooling, non-root user
# ---------------------------------------------------------------------------------------------
FROM python:3.14-slim-trixie@sha256:35caba83af2fe6da9a5d922f9b0156f21d1246a48278c58a08bd253303ba42e8 AS runtime

LABEL org.opencontainers.image.title="nuvexa-backend" \
      org.opencontainers.image.description="NUVEXA-AutoTask-AI-Agent API / worker / migrations" \
      org.opencontainers.image.vendor="Elarxsys" \
      org.opencontainers.image.licenses="Proprietary"

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PATH="/app/.venv/bin:${PATH}"

# Security: apply available Debian security updates, and remove pip from the system interpreter — the app
# runs from the uv-built virtualenv, and pip's vendored urllib3/msgpack copies carry known CVEs (Trivy).
RUN apt-get update \
 && apt-get upgrade -y --no-install-recommends \
 && rm -rf /var/lib/apt/lists/* \
 && python -m pip uninstall -y pip \
 && rm -rf /usr/local/lib/python3.14/site-packages/pip* /usr/local/bin/pip* \
 && groupadd --system --gid 10001 nuvexa \
 && useradd --system --uid 10001 --gid nuvexa --create-home --home-dir /home/nuvexa \
      --shell /usr/sbin/nologin nuvexa

WORKDIR /app

# Code and virtualenv stay root-owned (read-only for the runtime user).
COPY --from=builder /app /app

# Writable location for `nuvexa demo` (SQLite at ./data/nuvexa-demo.db). Unused with PostgreSQL.
RUN mkdir -p /app/data && chown nuvexa:nuvexa /app/data

USER 10001:10001

EXPOSE 8000

# Liveness only (no DB check). The worker has no HTTP port: disable this healthcheck for it
# (docker-compose.yml does). Azure Container Apps ignores HEALTHCHECK and uses its own probes.
HEALTHCHECK --interval=30s --timeout=5s --start-period=30s --retries=3 \
  CMD ["python", "-c", "import sys, urllib.request; r = urllib.request.urlopen('http://127.0.0.1:8000/api/v1/health/live', timeout=4); sys.exit(0 if r.status == 200 else 1)"]

CMD ["nuvexa", "api", "--host", "0.0.0.0", "--port", "8000"]
