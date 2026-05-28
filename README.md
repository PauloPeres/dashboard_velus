# Velus Dashboard

Dashboard gerencial multi-tenant da Velus — ISP regional de Bauru/Marília — combinando análises operacionais/financeiras com simuladores de decisão (PJ vs CLT, ajustes salariais, sindicato, split Simples Nacional).

> **Convenções de desenvolvimento** ficam em [`AGENT.md`](AGENT.md) — leia antes de codar.

**Status atual:** Fase 0.A concluída — fundação multi-tenant + ports & adapters + sync ponta-a-ponta com IXC e Fake adapters. **35/35 testes passando**. Próximas fases: dashboards Plotly, simuladores financeiros, deploy K8s.

## Stack

- **Backend:** Django 5.2 + Celery + Postgres 16 + Redis 7
- **Frontend:** Django templates + HTMX + Alpine.js + Plotly + Tailwind CSS
- **Auth:** Google OAuth via `django-allauth`
- **Infra:** Kubernetes (Helm) — local em Docker Compose
- **Tenancy:** shared schema, `organization_id` em todo model de domínio
- **Integrações externas:** Ports & Adapters (hexagonal) — IXC é apenas um adapter

## Pré-requisitos

- macOS / Linux
- [`uv`](https://docs.astral.sh/uv/) (`brew install uv`)
- Docker + Docker Compose
- Python 3.12 (gerenciado pelo uv automaticamente)

## Setup local

```bash
# 1. Clone e entre
git clone git@github.com:PauloPeres/dashboard_velus.git
cd dashboard_velus

# 2. Variáveis de ambiente
cp .env.example .env
# Edite .env — gere FERNET_KEY com:
python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"

# 3. Dependências (uv cria venv automaticamente)
uv sync

# 4. Subir Postgres + Redis
docker compose up -d

# 5. Aplicar migrations
uv run python manage.py migrate

# 6. Criar primeira Organization (tenant) com credenciais IXC já configuradas
uv run python manage.py create_organization velus \
    --name="Velus" \
    --owner-email=p.peresjr@gmail.com \
    --ixc-base-url=https://erp.example.com.br \
    --ixc-user-id=1 \
    --ixc-token=SEU-TOKEN-AQUI

# Sem credenciais IXC ainda? Omita os 3 últimos args — depois configura via admin
# ou rerodando o mesmo comando passando os args (idempotente).

# 7. Rodar Django
uv run python manage.py runserver

# 8. (outra aba) Worker Celery
uv run celery -A config worker --loglevel=info -Q tenant_velus,celery

# 9. (outra aba) Celery Beat (scheduler de sync incremental)
uv run celery -A config beat --loglevel=info
```

## Disparar um sync manual

```bash
# Via shell — útil pra primeiro bootstrap ou debug:
uv run python manage.py shell -c "
from apps.sync.tasks import sync_capability
from apps.tenancy.models import Organization
org = Organization.objects.get(slug='velus')
result = sync_capability(organization_id=org.pk, capability='CUSTOMERS', mode='BOOTSTRAP')
print(result)
"
```

## Testes

```bash
uv run pytest                       # tudo (35 testes, ~1.5s)
uv run pytest -m "not integration"  # exclui testes lentos
uv run pytest -m e2e                # só end-to-end com FakeSource
uv run pytest --cov                 # com cobertura
```

**Cobertura atual:**
- 4 testes de tenant isolation (TenantManager + decorator + cross-context)
- 5 testes e2e de sync (bootstrap, incremental, checkpoint, cross-tenant)
- 2 testes de criptografia Fernet (roundtrip + bytes encriptados no DB)
- 10 testes de value objects (Money + Percentage)
- 14 testes do adapter IXC (Pydantic schema + paginação + Anti-Corruption Layer)

## Qualidade de código

```bash
uv run ruff check .              # lint
uv run ruff format .             # format
uv run mypy apps/                # type check
uv run pre-commit run --all-files
```

## Arquitetura

Resumo (detalhes em [`AGENT.md`](AGENT.md)):

```
apps/
├── shared/         ✅ kernel: TenantModel, TenantManager, EncryptedField, Money/Percentage VOs
├── tenancy/        ✅ Organization, User, Membership, DataSource + create_organization command
├── customers/      ✅ Customer (domain DTO + Port + Repository + Model)
├── sync/           ✅ SyncJob, SyncCheckpoint, dispatch task por capability
├── integrations/
│   ├── shared/     ✅ BaseHttpAdapter (retry/throttle/auth), SourceRegistry, enums
│   ├── ixc/        ✅ IxcCustomerSource + Pydantic schemas (Anti-Corruption Layer)
│   ├── fake/       ✅ FakeCustomerSource in-memory
│   ├── sgp/        ⏳ placeholder
│   └── contaazul/  ⏳ placeholder
├── financial/      ⏳ Invoice, Payment + ports (Fase 0.B)
├── analytics/      ⏳ fact_* / dim_* + MRR/churn/ARPU (Fase 1)
├── scenarios/      ⏳ simuladores PJ vs CLT, sindicato, split CNPJ (Fase 2)
└── dashboards/     ⏳ views Plotly + exports PDF (Fase 1)
```

**Princípio:** bounded contexts de negócio (`customers`, `financial`, `analytics`, `scenarios`) **nunca importam adapters** — conversam por ports definidos no próprio domain, resolvidos pelo `SourceRegistry`.

## Operação

- **Sync incremental:** Celery Beat dispara a cada 3h por org/capability
- **Bootstrap:** Kubernetes Job pra cada nova org — paralelo controlado, idempotente, retomável
- **Monitoramento:** página `/admin/sync/status` mostra última execução por (org, source, capability)

## Segurança

- Credenciais externas (IXC, etc.) criptografadas no DB com **Fernet** — chave em K8s Secret separado
- Tenant isolation via `TenantManager` — todo query filtra por `organization_id` automático
- Google OAuth com allowlist por Organization
- CSP estrito, HSTS, CSRF, `django-axes` pra rate limit

## Licença

Proprietária — uso interno Velus. Contato: [p.peresjr@gmail.com](mailto:p.peresjr@gmail.com)
