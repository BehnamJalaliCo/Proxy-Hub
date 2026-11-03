# Proxy-Hub panel image (api, worker, migrate). Tests run in the "test" target against real
# PostgreSQL/Redis containers (deploy/docker-compose.test.yml); decision D10: no fakes.
# Pinned by digest; a registry mirror of the SAME digest may be passed as PYTHON_IMAGE / NODE_IMAGE
# (compose: PROXYHUB_PYTHON_IMAGE / PROXYHUB_NODE_IMAGE).
ARG PYTHON_IMAGE=python:3.13-slim-bookworm@sha256:281d39ff02e3b389401586134ff9e6572954687822a58055ec07a60ded4e0b7f
# Node only builds the owner dashboard (dashboard/); nothing of it runs in the final image.
# node:22-bookworm-slim, digest resolved from the Docker Hub registry on 2026-11-03 (Node 22.23.4).
ARG NODE_IMAGE=node:22-bookworm-slim@sha256:4b150b48b4116e2a22f2c39cc27025fbf4f153e2078fd3b14d159fdb5c7279da

# The dashboard sources are outside this build context (it is panel/). They come in as the named
# build context "dashboard": compose `build.additional_contexts: {dashboard: ../dashboard}`, or
# `docker build --build-context dashboard=dashboard panel`. Without it this empty stage is used and
# the runtime build stops at "lstat /package.json: no such file or directory" (never an image
# without its dashboard).
FROM scratch AS dashboard

FROM ${NODE_IMAGE} AS dashboard-build
ENV NPM_CONFIG_UPDATE_NOTIFIER=false NPM_CONFIG_FUND=false NPM_CONFIG_AUDIT=false
WORKDIR /dashboard
COPY --from=dashboard package.json package-lock.json ./
RUN npm ci
COPY --from=dashboard index.html vite.config.ts tsconfig.json tsconfig.app.json tsconfig.node.json tsconfig.test.json ./
COPY --from=dashboard public ./public
COPY --from=dashboard src ./src
RUN npm run build

FROM ${PYTHON_IMAGE} AS base
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy \
    UV_PROJECT_ENVIRONMENT=/app/.venv PATH="/app/.venv/bin:$PATH"
RUN pip install --no-cache-dir uv==0.11.32 \
    && useradd --uid 10001 --create-home --shell /usr/sbin/nologin app
WORKDIR /app
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project
COPY proxyhub ./proxyhub
RUN uv sync --frozen --no-dev

FROM base AS test
RUN uv sync --frozen
COPY tests ./tests
# /app is read-only for uid 10001: keep tool caches in /tmp.
ENV HYPOTHESIS_STORAGE_DIRECTORY=/tmp/.hypothesis MYPY_CACHE_DIR=/tmp/.mypy_cache RUFF_CACHE_DIR=/tmp/.ruff_cache
USER 10001
CMD ["sh", "-c", "ruff check proxyhub tests && ruff format --check proxyhub tests && mypy proxyhub && pytest -q -p no:cacheprovider"]

FROM base AS runtime
ARG GIT_COMMIT=unknown
ENV PROXYHUB_GIT_COMMIT=$GIT_COMMIT
# Served by proxyhub/api/routes_zz_static.py (read-only for uid 10001).
COPY --from=dashboard-build /dashboard/dist /app/dashboard
USER 10001
HEALTHCHECK --interval=30s --timeout=5s --retries=3 \
  CMD python -c "import sys,urllib.request; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/livez', timeout=4).status == 200 else 1)" || exit 1
CMD ["python", "-m", "proxyhub", "api"]
