"""Settings de desenvolvimento local — DEBUG, debug toolbar, console email."""

from __future__ import annotations

from .base import *  # noqa: F403
from .base import INSTALLED_APPS, MIDDLEWARE

DEBUG = True

# -----------------------------------------------------------------------------
# Debug toolbar
# -----------------------------------------------------------------------------
INSTALLED_APPS = [*INSTALLED_APPS, "debug_toolbar", "django_extensions"]
MIDDLEWARE = [
    "debug_toolbar.middleware.DebugToolbarMiddleware",
    *MIDDLEWARE,
]
INTERNAL_IPS = ["127.0.0.1", "localhost"]

# -----------------------------------------------------------------------------
# Sessões mais permissivas em dev (HTTPS não obrigatório)
# -----------------------------------------------------------------------------
SESSION_COOKIE_SECURE = False
CSRF_COOKIE_SECURE = False

# -----------------------------------------------------------------------------
# Email — print no console
# -----------------------------------------------------------------------------
EMAIL_BACKEND = "django.core.mail.backends.console.EmailBackend"

# -----------------------------------------------------------------------------
# allauth — relaxar verificação de email em dev
# -----------------------------------------------------------------------------
ACCOUNT_EMAIL_VERIFICATION = "optional"
