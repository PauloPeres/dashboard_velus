# =============================================================================
# Velus Dashboard — Dockerfile
# =============================================================================
# Single Dockerfile usado por web, worker e beat (CMD sobrescrito no compose).
# Multi-stage: deps em layer separado pra cachear instalação.
#
# Build args:
#   PYTHON_VERSION (default 3.12)
#   UV_VERSION (default 0.11.16)
# =============================================================================

ARG PYTHON_VERSION=3.12

# -----------------------------------------------------------------------------
# Stage 0: uv binary — aliasado pra permitir COPY --from sem variable expansion
# -----------------------------------------------------------------------------
FROM ghcr.io/astral-sh/uv:0.11.16 AS uv_bin

# -----------------------------------------------------------------------------
# Stage 1: deps — instala uv + system deps + Python deps
# -----------------------------------------------------------------------------
FROM python:${PYTHON_VERSION}-slim AS deps

# uv via copy do binário oficial — não polui apt
COPY --from=uv_bin /uv /usr/local/bin/uv

ENV UV_LINK_MODE=copy \
    UV_COMPILE_BYTECODE=1 \
    UV_PROJECT_ENVIRONMENT=/opt/venv \
    PATH="/opt/venv/bin:$PATH" \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

# System deps mínimos pra build (psycopg, cryptography, etc.)
RUN apt-get update && apt-get install -y --no-install-recommends \
        build-essential libpq-dev curl ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# Lockfiles primeiro pra cachear o layer de deps
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-install-project --no-dev

# -----------------------------------------------------------------------------
# Stage 2: app — copia código fonte + dev deps opcionais
# -----------------------------------------------------------------------------
FROM deps AS app

# Dev deps opcionais via build arg
ARG INSTALL_DEV=false
RUN if [ "$INSTALL_DEV" = "true" ]; then \
        uv sync --frozen --no-install-project; \
    fi

# Copia código (tudo que não está no .dockerignore)
COPY . /app

# Collectstatic — gera /app/staticfiles pronto pra ser servido pelo Whitenoise
# em produção. Em dev, runserver com DEBUG=True serve direto (e settings/development.py
# override do STORAGES dispensa o manifest do Whitenoise).
# Usamos vars dummy pra Settings carregar mesmo sem env real no build.
RUN DJANGO_SETTINGS_MODULE=config.settings.development \
    DJANGO_SECRET_KEY="build-time-only" \
    DATABASE_URL="postgres://x:x@localhost/build" \
    FERNET_KEY="build-time-only-build-time-only-build-time" \
    python manage.py collectstatic --noinput --clear 2>/dev/null || true

# User não-root pra segurança em produção
RUN useradd --create-home --shell /bin/bash velus \
    && chown -R velus:velus /app /opt/venv
USER velus

EXPOSE 8000

# Default = runserver (dev). Compose sobrescreve com gunicorn em prod.
CMD ["python", "manage.py", "runserver", "0.0.0.0:8000"]
