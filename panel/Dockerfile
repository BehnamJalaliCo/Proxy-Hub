# Proxy-Hub panel image (api, worker, migrate). Tests run in the "test" target against real
# PostgreSQL/Redis containers (deploy/docker-compose.test.yml); decision D10: no fakes.
FROM python:3.13-slim-bookworm@sha256:281d39ff02e3b389401586134ff9e6572954687822a58055ec07a60ded4e0b7f AS base
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
USER 10001
HEALTHCHECK --interval=30s --timeout=5s --retries=3 \
  CMD python -c "import sys,urllib.request; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/livez', timeout=4).status == 200 else 1)" || exit 1
CMD ["python", "-m", "proxyhub", "api"]
