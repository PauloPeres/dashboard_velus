"""Settings de teste — rápido, em memória onde possível, Celery eager."""

from __future__ import annotations

from .base import *  # noqa: F403

DEBUG = False

# -----------------------------------------------------------------------------
# Password hashing rápido (não-seguro — só pra teste)
# -----------------------------------------------------------------------------
PASSWORD_HASHERS = ["django.contrib.auth.hashers.MD5PasswordHasher"]

# -----------------------------------------------------------------------------
# Cache em memória — sem Redis
# -----------------------------------------------------------------------------
CACHES = {
    "default": {
        "BACKEND": "django.core.cache.backends.locmem.LocMemCache",
        "LOCATION": "test",
    }
}

# -----------------------------------------------------------------------------
# Celery — executa síncrono na mesma thread
# -----------------------------------------------------------------------------
CELERY_TASK_ALWAYS_EAGER = True
CELERY_TASK_EAGER_PROPAGATES = True

# -----------------------------------------------------------------------------
# Email — não enviar
# -----------------------------------------------------------------------------
EMAIL_BACKEND = "django.core.mail.backends.locmem.EmailBackend"

# -----------------------------------------------------------------------------
# allauth — não bloquear teste
# -----------------------------------------------------------------------------
ACCOUNT_EMAIL_VERIFICATION = "none"

# -----------------------------------------------------------------------------
# Logging silencioso em teste
# -----------------------------------------------------------------------------
LOGGING = {
    "version": 1,
    "disable_existing_loggers": True,
    "handlers": {"null": {"class": "logging.NullHandler"}},
    "root": {"handlers": ["null"], "level": "CRITICAL"},
}

# -----------------------------------------------------------------------------
# Sessões — sem secure (HTTP em CI)
# -----------------------------------------------------------------------------
SESSION_COOKIE_SECURE = False
CSRF_COOKIE_SECURE = False
