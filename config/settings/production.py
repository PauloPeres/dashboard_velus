"""Settings de produção — hardening completo, falha alto se config faltar."""

from __future__ import annotations

from .base import *  # noqa: F403
from .base import ALLOWED_HOSTS, EMAIL_BACKEND, EMAIL_ENABLED, env

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
# Email (#95) — alerta alto, sem derrubar o boot
# =============================================================================
# Decisão: e-mail faltando NÃO faz `raise`. O dashboard é read-only e continua
# útil sem e-mail; derrubar o Deployment inteiro (crashloop) porque o Secret do
# Mailgun ainda não foi injetado troca um problema silencioso por indisponibi-
# lidade total — inclusive no próprio deploy desta mudança. O que não pode
# continuar é o silêncio: loga em ERROR no boot (logging JSON → agregador) e o
# `manage.py send_test_email` mostra o backend ativo na hora de validar.
if not EMAIL_ENABLED:
    import logging

    logging.getLogger("config.settings").error(
        "EMAIL NAO CONFIGURADO EM PRODUCAO: MAILGUN_API_KEY/MAILGUN_SENDER_DOMAIN "
        "ausentes — convites, reset de senha e digest de churn vao para o stdout "
        "do pod (backend=%s) e NAO serao entregues.",
        EMAIL_BACKEND,
    )

# Links de reset/convite montados fora de request (task Celery) precisam de
# https — sem isso o allauth monta http:// e o link sai inseguro.
ACCOUNT_DEFAULT_HTTP_PROTOCOL = "https"

# =============================================================================
# HTTPS / HSTS — assume TLS no ingress, proxy passa X-Forwarded-Proto
# =============================================================================
SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")
SECURE_SSL_REDIRECT = True
SECURE_HSTS_SECONDS = 60 * 60 * 24 * 365  # 1 ano
SECURE_HSTS_INCLUDE_SUBDOMAINS = True
SECURE_HSTS_PRELOAD = True

# Probes do K8s batem em /health/ via HTTP interno (sem X-Forwarded-Proto);
# isenta do redirect 301->https pra devolver 200 limpo.
SECURE_REDIRECT_EXEMPT = [r"^health/?$"]

# CSRF atrás do proxy TLS: POSTs (admin/login) precisam da origin https confiável.
# Deriva de ALLOWED_HOSTS pra não duplicar o domínio.
CSRF_TRUSTED_ORIGINS = [f"https://{host}" for host in ALLOWED_HOSTS]

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
