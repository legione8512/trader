# Python 3.12 in the container even though local development may use 3.13.
# The container is the source of truth (Phase 0 decision OD-10).
FROM python:3.12-slim AS base

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /srv

# Dependency layer first: it changes rarely, so Docker can reuse the cached
# layer across every source-code change.
COPY pyproject.toml ./
RUN python -m pip install --upgrade pip \
 && mkdir -p app \
 && touch app/__init__.py \
 && pip install .

# Application layer.
COPY app ./app
COPY alembic ./alembic
COPY alembic.ini ./
RUN pip install --no-deps .

# Run as a non-root user. A container process that does not need root must not
# have it: it limits the damage of any remote code execution.
RUN useradd --create-home --uid 10001 appuser \
 && chown -R appuser:appuser /srv
USER appuser

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/api/health')"

# Migrations are NOT run here. In an application that moves money, a schema
# change must be an explicit operator action, never a side effect of a restart:
#   docker compose run --rm backend alembic upgrade head
CMD ["uvicorn", "app.main:create_app", "--factory", "--host", "0.0.0.0", "--port", "8000"]
