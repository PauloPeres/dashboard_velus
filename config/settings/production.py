"""Settings de produção — hardening completo, falha alto se config faltar."""

from __future__ import annotations

from .base import *  # noqa: F403
from .base import ALLOWED_HOSTS, env

# =============================================================================
# Falhas de segurança configuráveis — bloqueia subir sem o necessário
# =============================================================================
if env.DJANGO_DEBUG:
    raise RuntimeError("DJANGO_DEBUG=True não pode ser usado em produção.")

if not ALLOWED_HOSTS or ALLOWED_HOSTS == ["localhost", "127.0.0.1"]:
    raise RuntimeError(
        "ALLOWED_HOSTS precisa ser configurado explicitamente em produção."
    )

if env.DJANGO_SECRET_KEY.get_secret_value() == "change-me-dev-only-not-for-prod":
    raise RuntimeError("DJANGO_SECRET_KEY precisa ser gerada para produção.")

if env.FERNET_KEY.get_secret_value() == "change-me-generate-with-fernet":
    raise RuntimeError("FERNET_KEY precisa ser gerada para produção.")

DEBUG = False

# =============================================================================
# HTTPS / HSTS — assume TLS no ingress, proxy passa X-Forwarded-Proto
# =============================================================================
SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")
SECURE_SSL_REDIRECT = True
SECURE_HSTS_SECONDS = 60 * 60 * 24 * 365  # 1 ano
SECURE_HSTS_INCLUDE_SUBDOMAINS = True
SECURE_HSTS_PRELOAD = True

# =============================================================================
# Cookies — secure-only, sem leak entre subdomains
# =============================================================================
SESSION_COOKIE_SECURE = True
CSRF_COOKIE_SECURE = True

# =============================================================================
# Logging em JSON (override do base.py — força JSON em prod independente de env)
# =============================================================================
import structlog  # noqa: E402

LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "formatters": {
        "json_formatter": {
            "()": structlog.stdlib.ProcessorFormatter,
            "processor": structlog.processors.JSONRenderer(),
            "foreign_pre_chain": [
                structlog.contextvars.merge_contextvars,
                structlog.processors.add_log_level,
                structlog.processors.TimeStamper(fmt="iso"),
            ],
        },
    },
    "handlers": {
        "console": {
            "class": "logging.StreamHandler",
            "formatter": "json_formatter",
        },
    },
    "root": {
        "handlers": ["console"],
        "level": env.LOG_LEVEL,
    },
    "loggers": {
        "django.db.backends": {"level": "WARNING", "propagate": True},
        "django.request": {"level": "WARNING", "propagate": True},
    },
}

# =============================================================================
# Sentry (opcional)
# =============================================================================
if env.SENTRY_DSN:
    try:
        import sentry_sdk
        from sentry_sdk.integrations.celery import CeleryIntegration
        from sentry_sdk.integrations.django import DjangoIntegration

        sentry_sdk.init(
            dsn=env.SENTRY_DSN,
            integrations=[DjangoIntegration(), CeleryIntegration()],
            traces_sample_rate=0.05,
            send_default_pii=False,
            environment="production",
        )
    except ImportError:
        # sentry-sdk não está nas deps base — instalável separado quando necessário
        pass
