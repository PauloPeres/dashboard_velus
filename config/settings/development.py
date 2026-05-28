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

# -----------------------------------------------------------------------------
# Static files — sem manifest em dev (runserver serve direto)
# Em produção, `python manage.py collectstatic` roda no build do Dockerfile
# e o CompressedManifestStaticFilesStorage do base.py vale.
# -----------------------------------------------------------------------------
STORAGES = {
    "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
    "staticfiles": {"BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"},
}

# -----------------------------------------------------------------------------
# CSP em dev — permite CDNs (Plotly, Tailwind, HTMX) + inline scripts
# Em produção, Tailwind compilado + Plotly self-host + nonces inline.
# -----------------------------------------------------------------------------
CONTENT_SECURITY_POLICY = {
    "DIRECTIVES": {
        "default-src": ["'self'"],
        "script-src": [
            "'self'", "'unsafe-inline'", "'unsafe-eval'",
            "https://cdn.plot.ly", "https://cdn.tailwindcss.com",
            "https://unpkg.com",
        ],
        "style-src": ["'self'", "'unsafe-inline'", "https://cdn.tailwindcss.com"],
        "img-src": ["'self'", "data:"],
        "font-src": ["'self'", "data:"],
        "connect-src": ["'self'"],
        "frame-ancestors": ["'none'"],
        "base-uri": ["'self'"],
        "form-action": ["'self'"],
    },
}
