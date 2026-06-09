"""Settings base — comum a todos os ambientes.

Cada environment-specific (development.py, production.py, test.py) importa
deste arquivo e sobrescreve o necessário.
"""

from __future__ import annotations

from pathlib import Path
from urllib.parse import urlparse

from ._env import env

# =============================================================================
# Paths
# =============================================================================
BASE_DIR = Path(__file__).resolve().parent.parent.parent

# =============================================================================
# Django core
# =============================================================================
SECRET_KEY = env.DJANGO_SECRET_KEY.get_secret_value()
DEBUG = env.DJANGO_DEBUG
ALLOWED_HOSTS = [h.strip() for h in env.DJANGO_ALLOWED_HOSTS.split(",") if h.strip()]

SITE_ID = 1

# =============================================================================
# Apps — local apps serão adicionados conforme implementados
# =============================================================================
DJANGO_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "django.contrib.sites",
    "django.contrib.humanize",  # filters intcomma, naturaltime, etc.
]

THIRD_PARTY_APPS = [
    "allauth",
    "allauth.account",
    "allauth.socialaccount",
    "allauth.socialaccount.providers.google",
    "axes",
    "simple_history",
    "django_structlog",
]

LOCAL_APPS: list[str] = [
    "apps.shared",
    "apps.tenancy",
    "apps.integrations.shared",
    "apps.customers",
    "apps.financial",
    "apps.helpdesk",
    "apps.atendimento",
    "apps.network",
    "apps.inventory",
    "apps.sales",
    "apps.analytics",
    "apps.integrations.fake",
    "apps.integrations.ixc",
    "apps.integrations.opa",
    "apps.sync",
    "apps.dashboards",
    "apps.scenarios",
    "apps.mcp",
]

INSTALLED_APPS = DJANGO_APPS + THIRD_PARTY_APPS + LOCAL_APPS

# =============================================================================
# Middleware — ordem importa
# =============================================================================
MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "whitenoise.middleware.WhiteNoiseMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.locale.LocaleMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django_structlog.middlewares.RequestMiddleware",
    "allauth.account.middleware.AccountMiddleware",
    "apps.shared.middleware.TenantMiddleware",  # injeta organization no contextvar
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
    "csp.middleware.CSPMiddleware",
    "simple_history.middleware.HistoryRequestMiddleware",
    "axes.middleware.AxesMiddleware",
]

ROOT_URLCONF = "config.urls"

# =============================================================================
# Templates
# =============================================================================
TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [BASE_DIR / "templates"],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.debug",
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
                "apps.shared.context_processors.tenant",
                "apps.dashboards.context_processors.period_context",
            ],
        },
    },
]

WSGI_APPLICATION = "config.wsgi.application"
ASGI_APPLICATION = "config.asgi.application"

# =============================================================================
# Database — parse manual de DATABASE_URL (postgres://user:pass@host:port/db)
# =============================================================================
_db_url = urlparse(env.DATABASE_URL)
DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.postgresql",
        "NAME": _db_url.path.lstrip("/"),
        "USER": _db_url.username or "",
        "PASSWORD": _db_url.password or "",
        "HOST": _db_url.hostname or "localhost",
        "PORT": str(_db_url.port or 5432),
        "CONN_MAX_AGE": 60,
        "OPTIONS": {
            "connect_timeout": 5,
        },
    }
}

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

# =============================================================================
# Cache (Redis)
# =============================================================================
CACHES = {
    "default": {
        "BACKEND": "django.core.cache.backends.redis.RedisCache",
        "LOCATION": env.REDIS_URL,
        "TIMEOUT": 60 * 5,  # 5 min default
    }
}

# =============================================================================
# Auth
# =============================================================================
AUTH_USER_MODEL = "tenancy.User"

AUTHENTICATION_BACKENDS = [
    "axes.backends.AxesStandaloneBackend",  # PRIMEIRO — rate limit
    "django.contrib.auth.backends.ModelBackend",
    "allauth.account.auth_backends.AuthenticationBackend",
]

LOGIN_URL = "/accounts/login/"
LOGIN_REDIRECT_URL = "/"
LOGOUT_REDIRECT_URL = "/accounts/login/"

# -----------------------------------------------------------------------------
# django-allauth — email+senha (+ Google OAuth opcional)
# -----------------------------------------------------------------------------
ACCOUNT_USER_MODEL_USERNAME_FIELD = None
ACCOUNT_LOGIN_METHODS = {"email"}
# email* + password1* garante que o LoginForm inclui o campo de senha
# (sem password1 o allauth 65 remove o campo e ativa o fluxo code-based)
ACCOUNT_SIGNUP_FIELDS = ["email*", "password1*", "password2*"]
ACCOUNT_EMAIL_VERIFICATION = "optional"
ACCOUNT_SESSION_REMEMBER = False
ACCOUNT_UNIQUE_EMAIL = True
# Signup fechado — só admin cria contas. Google OAuth só funciona para
# emails já cadastrados (adapter vincula automaticamente).
ACCOUNT_ADAPTER = "apps.tenancy.adapters.NoSignupAccountAdapter"
SOCIALACCOUNT_ADAPTER = "apps.tenancy.adapters.AutoConnectSocialAdapter"
# Desabilita o "Login by Code" (passwordless via email) introduzido no allauth 0.63+
# — queremos email+senha clássico, não OTP por email
ACCOUNT_LOGIN_BY_CODE_ENABLED = False
ACCOUNT_LOGIN_BY_CODE_REQUIRED = False
SOCIALACCOUNT_AUTO_SIGNUP = False
SOCIALACCOUNT_EMAIL_AUTHENTICATION = True
SOCIALACCOUNT_EMAIL_AUTHENTICATION_AUTO_CONNECT = True
SOCIALACCOUNT_LOGIN_ON_GET = True
SOCIALACCOUNT_PROVIDERS = {
    "google": {
        "APP": {
            "client_id": env.GOOGLE_OAUTH_CLIENT_ID,
            "secret": env.GOOGLE_OAUTH_CLIENT_SECRET.get_secret_value(),
            "key": "",
        },
        "SCOPE": ["profile", "email"],
        "AUTH_PARAMS": {"access_type": "online"},
    }
}

# -----------------------------------------------------------------------------
# django-axes — rate limit de login
# -----------------------------------------------------------------------------
AXES_FAILURE_LIMIT = 5
AXES_COOLOFF_TIME = 0.25  # 15 minutos
AXES_LOCKOUT_PARAMETERS = [["ip_address", "username"]]
AXES_RESET_ON_SUCCESS = True

# =============================================================================
# Password validation
# =============================================================================
AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"},
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator",
     "OPTIONS": {"min_length": 12}},
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
]

# =============================================================================
# Sessões — hardening (overrides em production.py)
# =============================================================================
SESSION_COOKIE_HTTPONLY = True
SESSION_COOKIE_SAMESITE = "Lax"
SESSION_COOKIE_AGE = 60 * 60 * 24  # 1 dia
SESSION_EXPIRE_AT_BROWSER_CLOSE = True

CSRF_COOKIE_HTTPONLY = False  # precisa ser False pra HTMX ler do cookie
CSRF_COOKIE_SAMESITE = "Lax"

# =============================================================================
# Internacionalização
# =============================================================================
LANGUAGE_CODE = env.DJANGO_LANGUAGE_CODE
TIME_ZONE = env.DJANGO_TIME_ZONE
USE_I18N = True
USE_TZ = True

# =============================================================================
# Static / Media
# =============================================================================
STATIC_URL = "/static/"
STATIC_ROOT = BASE_DIR / "staticfiles"
STATICFILES_DIRS = [BASE_DIR / "static"] if (BASE_DIR / "static").exists() else []

STORAGES = {
    "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
    "staticfiles": {"BACKEND": "whitenoise.storage.CompressedManifestStaticFilesStorage"},
}

MEDIA_URL = "/media/"
MEDIA_ROOT = BASE_DIR / "media"

# =============================================================================
# Email
# =============================================================================
EMAIL_BACKEND = env.EMAIL_BACKEND
DEFAULT_FROM_EMAIL = env.DEFAULT_FROM_EMAIL

# =============================================================================
# Celery
# =============================================================================
CELERY_BROKER_URL = env.CELERY_BROKER_URL
CELERY_RESULT_BACKEND = env.CELERY_RESULT_BACKEND
CELERY_ACCEPT_CONTENT = ["json"]
CELERY_TASK_SERIALIZER = "json"
CELERY_RESULT_SERIALIZER = "json"
CELERY_TIMEZONE = TIME_ZONE
CELERY_TASK_TRACK_STARTED = True
CELERY_TASK_TIME_LIMIT = 30 * 60        # 30 min hard
CELERY_TASK_SOFT_TIME_LIMIT = 25 * 60   # 25 min soft
CELERY_WORKER_PREFETCH_MULTIPLIER = 1   # crítico: não monopolizar tenant em bootstrap
CELERY_TASK_ACKS_LATE = True            # task só ack após executar — robusto a crash
CELERY_WORKER_MAX_TASKS_PER_CHILD = 1000  # restart periódico p/ evitar memory leak

# Filas por tenant — populadas dinamicamente; fila default 'celery' sempre existe.
CELERY_TASK_DEFAULT_QUEUE = "celery"

# Beat schedule — sync incremental pra TODAS as orgs ativas.
# Task `dispatch_incremental_for_all_orgs` itera org-by-org e enfileira
# uma sub-task por (org, capability) na fila do tenant.
# Rodar Beat: `uv run celery -A config beat --loglevel=info`
#
# Capabilities escalonadas em janelas distintas (minutos/horas diferentes) pra
# não disparar as 11 sincronizações ao mesmo tempo e sobrecarregar a API do IXC:
#   - core (clientes/contratos): a cada 3h, :00 — base mais crítica
#   - financeiro (faturas/pagamentos/despesas): a cada 3h, :20
#   - suporte (chamados/equipamentos): a cada 3h, :40
#   - rede (conexões/banda): a cada 6h — volume alto, menos crítico
#   - CRM (leads/negociações): a cada 6h
from celery.schedules import crontab  # noqa: E402

CELERY_BEAT_SCHEDULE: dict = {
    "sync-core-every-3h": {
        "task": "apps.sync.tasks.dispatch_incremental_for_all_orgs",
        "schedule": crontab(minute=0, hour="*/3"),
        "kwargs": {"capabilities": ["CUSTOMERS", "CONTRACTS"]},
        "options": {"queue": "celery"},
    },
    "sync-financial-every-3h": {
        "task": "apps.sync.tasks.dispatch_incremental_for_all_orgs",
        "schedule": crontab(minute=20, hour="*/3"),
        "kwargs": {"capabilities": ["INVOICES", "PAYMENTS", "EXPENSES"]},
        "options": {"queue": "celery"},
    },
    "sync-support-every-3h": {
        "task": "apps.sync.tasks.dispatch_incremental_for_all_orgs",
        "schedule": crontab(minute=40, hour="*/3"),
        "kwargs": {"capabilities": ["TICKETS", "EQUIPMENT"]},
        "options": {"queue": "celery"},
    },
    "sync-network-every-6h": {
        "task": "apps.sync.tasks.dispatch_incremental_for_all_orgs",
        "schedule": crontab(minute=10, hour="1,7,13,19"),
        "kwargs": {"capabilities": ["CONNECTIONS", "BANDWIDTH"]},
        "options": {"queue": "celery"},
    },
    "sync-crm-every-6h": {
        "task": "apps.sync.tasks.dispatch_incremental_for_all_orgs",
        "schedule": crontab(minute=50, hour="2,8,14,20"),
        "kwargs": {"capabilities": ["LEADS", "OPPORTUNITIES"]},
        "options": {"queue": "celery"},
    },
    # Opa! Suite (atendimento) tem fluxo dedicado fora do dispatch genérico.
    # 1x/dia, fora do horário comercial — atendimento não precisa ser realtime.
    "sync-opa-atendimento-daily": {
        "task": "apps.atendimento.tasks.sync_opa_for_all_orgs",
        "schedule": crontab(minute=0, hour=5),  # 05:00 todo dia
        "options": {"queue": "celery"},
    },
    "sync-plano-contas-daily": {
        "task": "apps.analytics.tasks.dispatch_plano_contas_for_all_orgs",
        "schedule": crontab(minute=30, hour=3),  # 03:30 todo dia
        "options": {"queue": "celery"},
    },
    "rebuild-financial-facts-daily": {
        "task": "apps.analytics.tasks.dispatch_fact_rebuild_for_all_orgs",
        "schedule": crontab(minute=40, hour=3),  # 03:40 — rede de segurança
        "options": {"queue": "celery"},
    },
    "reconcile-financial-daily": {
        # 01:00 SP (CELERY_TIMEZONE = America/Sao_Paulo). Pull completo de
        # pagamentos/despesas pra soft-delete dos que sumiram do IXC.
        "task": "apps.sync.tasks.dispatch_reconciliation_for_all_orgs",
        "schedule": crontab(minute=0, hour=1),
        "kwargs": {"capabilities": ["PAYMENTS", "EXPENSES"]},
        "options": {"queue": "celery"},
    },
    "capture-network-snapshot-every-3h": {
        "task": "apps.analytics.tasks.dispatch_network_snapshot_for_all_orgs",
        "schedule": crontab(minute=15, hour="*/3"),  # foto de rede a cada 3h
        "options": {"queue": "celery"},
    },
    "compute-churn-risk-daily": {
        "task": "apps.analytics.tasks.dispatch_churn_risk_for_all_orgs",
        "schedule": crontab(minute=0, hour=4),  # 04:00 todo dia
        "options": {"queue": "celery"},
    },
    "train-churn-ml-weekly": {
        "task": "apps.analytics.tasks.dispatch_churn_ml_train_for_all_orgs",
        "schedule": crontab(minute=30, hour=2, day_of_week=1),  # seg 02:30
        "options": {"queue": "celery"},
    },
    "send-churn-digest-weekly": {
        "task": "apps.analytics.tasks.dispatch_churn_digest_weekly",
        "schedule": crontab(minute=0, hour=8, day_of_week=1),  # seg 08:00
        "options": {"queue": "celery"},
    },
    "send-churn-digest-monthly": {
        "task": "apps.analytics.tasks.dispatch_churn_digest_monthly",
        "schedule": crontab(minute=0, hour=8, day_of_month=1),  # dia 1, 08:00
        "options": {"queue": "celery"},
    },
    # IA supervisora de QA — roda após o sync Opa! (05:00), com PII redigida.
    # No-op enquanto QA_LLM_ENABLED estiver desligado (sem API key).
    "qa-reviews-daily": {
        "task": "apps.analytics.tasks.dispatch_qa_reviews_for_all_orgs",
        "schedule": crontab(minute=30, hour=6),  # 06:30 todo dia
        "options": {"queue": "celery"},
    },
}

# =============================================================================
# Logging — estruturado via structlog
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
        "plain_console": {
            "()": structlog.stdlib.ProcessorFormatter,
            "processor": structlog.dev.ConsoleRenderer(colors=True),
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
            "formatter": "plain_console" if env.LOG_FORMAT == "console" else "json_formatter",
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

structlog.configure(
    processors=[
        structlog.contextvars.merge_contextvars,
        structlog.stdlib.filter_by_level,
        structlog.processors.add_log_level,
        structlog.processors.TimeStamper(fmt="iso"),
        structlog.processors.StackInfoRenderer(),
        structlog.processors.format_exc_info,
        structlog.stdlib.ProcessorFormatter.wrap_for_formatter,
    ],
    logger_factory=structlog.stdlib.LoggerFactory(),
    wrapper_class=structlog.stdlib.BoundLogger,
    cache_logger_on_first_use=True,
)

# =============================================================================
# Security headers — defaults conservadores; production.py reforça
# =============================================================================
SECURE_CONTENT_TYPE_NOSNIFF = True
SECURE_BROWSER_XSS_FILTER = True
SECURE_REFERRER_POLICY = "same-origin"
X_FRAME_OPTIONS = "DENY"

# -----------------------------------------------------------------------------
# CSP (django-csp) — default deny, libera o mínimo
# -----------------------------------------------------------------------------
CONTENT_SECURITY_POLICY = {
    "DIRECTIVES": {
        "default-src": ["'self'"],
        "script-src": ["'self'", "'unsafe-inline'"],
        "style-src": ["'self'", "'unsafe-inline'"],  # Tailwind compilado pode ainda usar inline
        "img-src": ["'self'", "data:"],
        "font-src": ["'self'", "data:"],
        "connect-src": ["'self'"],
        "frame-ancestors": ["'none'"],
        "base-uri": ["'self'"],
        "form-action": ["'self'"],
    },
}

# =============================================================================
# Fernet key (consumido por apps.shared.fields.EncryptedField)
# =============================================================================
FERNET_KEY = env.FERNET_KEY.get_secret_value()

# =============================================================================
# Opa! Suite (atendimento/WhatsApp) — consumido por setup_opa_credentials
# =============================================================================
OPA_LINK = env.OPA_LINK
OPA_TOKEN = env.OPA_TOKEN.get_secret_value()

# =============================================================================
# IA supervisora de atendimento (LLM-as-judge) — consumida por apps.analytics
# =============================================================================
ANTHROPIC_API_KEY = env.ANTHROPIC_API_KEY.get_secret_value()
QA_LLM_MODEL = env.QA_LLM_MODEL
# QA só roda se habilitado E com chave presente — desligado é o default seguro.
QA_LLM_ENABLED = env.QA_LLM_ENABLED and bool(ANTHROPIC_API_KEY)

# =============================================================================
# Servidor MCP (read-only) — consumido por apps.mcp.server
# =============================================================================
MCP_ENABLED = env.MCP_ENABLED
MCP_HOST = env.MCP_HOST
MCP_PORT = env.MCP_PORT
MCP_ALLOWED_HOSTS = [h.strip() for h in env.MCP_ALLOWED_HOSTS.split(",") if h.strip()]
