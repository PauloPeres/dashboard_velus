# =============================================================================
# Velus Dashboard — Dockerfile
# =============================================================================
# Single Dockerfile usado por web, worker e beat (CMD sobrescrito no compose).
# Multi-stage: Tailwind CSS compilado em stage Node, deps Python em layer
# separado pra cachear instalação.
#
# Build args:
#   PYTHON_VERSION (default 3.12)
# =============================================================================

ARG PYTHON_VERSION=3.12

# -----------------------------------------------------------------------------
# Stage 0: css — compila Tailwind CSS + baixa vendor JS (Plotly, HTMX)
# -----------------------------------------------------------------------------
FROM node:20-alpine AS css

WORKDIR /build

# Config do Tailwind (cacheia layer se config não mudar)
COPY tailwind.config.js ./
COPY static/src/input.css ./static/src/

# Templates escaneados pelo Tailwind pra tree-shaking
COPY templates/ ./templates/
COPY apps/ ./apps/

# Instala Tailwind e compila CSS minificado
RUN npx tailwindcss@3 -i static/src/input.css -o static/css/tailwind.min.css --minify

# Baixa libs JS versionadas (self-host — elimina CDN e erros de CSP)
RUN mkdir -p static/vendor \
    && wget -qO static/vendor/plotly-2.35.2.min.js "https://cdn.plot.ly/plotly-2.35.2.min.js" \
    && wget -qO static/vendor/htmx-1.9.12.min.js "https://unpkg.com/htmx.org@1.9.12/dist/htmx.min.js"

# -----------------------------------------------------------------------------
# Stage 1: uv binary
# -----------------------------------------------------------------------------
FROM ghcr.io/astral-sh/uv:0.11.16 AS uv_bin

# -----------------------------------------------------------------------------
# Stage 2: deps — instala uv + system deps + Python deps
# -----------------------------------------------------------------------------
FROM python:${PYTHON_VERSION}-slim AS deps

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
# Stage 3: app — copia código fonte + assets compilados
# -----------------------------------------------------------------------------
FROM deps AS app

# Dev deps opcionais via build arg
ARG INSTALL_DEV=false
RUN if [ "$INSTALL_DEV" = "true" ]; then \
        uv sync --frozen --no-install-project; \
    fi

# Copia código (tudo que não está no .dockerignore)
COPY . /app

# Copia CSS compilado + vendor JS do stage css
COPY --from=css /build/static/css/tailwind.min.css /app/static/css/tailwind.min.css
COPY --from=css /build/static/vendor/ /app/static/vendor/

# Collectstatic — gera /app/staticfiles pronto pra ser servido pelo Whitenoise.
# Usamos vars dummy pra Settings carregar mesmo sem env real no build.
RUN DJANGO_SETTINGS_MODULE=config.settings.base \
    DJANGO_SECRET_KEY="build-time-only" \
    DATABASE_URL="postgres://x:x@localhost/build" \
    FERNET_KEY="change-me-generate-with-fernet" \
    python manage.py collectstatic --noinput --clear

# User não-root pra segurança em produção
RUN useradd --create-home --shell /bin/bash velus \
    && chown -R velus:velus /app /opt/venv
USER velus

EXPOSE 8000

# Default = runserver (dev). Compose sobrescreve com gunicorn em prod.
CMD ["python", "manage.py", "runserver", "0.0.0.0:8000"]
