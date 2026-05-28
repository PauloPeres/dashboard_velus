# Velus Dashboard

Dashboard gerencial multi-tenant da Velus — ISP regional de Bauru/Marília — combinando análises operacionais/financeiras com simuladores de decisão (PJ vs CLT, ajustes salariais, sindicato, split Simples Nacional).

> **Convenções de desenvolvimento** ficam em [`AGENT.md`](AGENT.md) — leia antes de codar.

**Status atual (2026-05-28):** Fases 0.A + 0.B + 1 + 2 + Sync UI todas mergeadas em `main`. **48/48 testes passando.** Pronto pra rodar contra IXC real assim que houver credenciais.

## Stack

- **Backend:** Django 5.2 + Celery 5.6 + Postgres 16 + Redis 7
- **Frontend:** Django templates + HTMX + Plotly + Tailwind CSS (CDN em dev)
- **Auth:** Google OAuth via `django-allauth` (allowlist por Organization)
- **Infra:** Docker Compose pra dev local; Kubernetes (Helm) pra produção
- **Tenancy:** shared schema, `organization_id` em todo model de domínio, `TenantManager` enforce automático
- **Integrações externas:** Ports & Adapters (hexagonal) — IXC é apenas um adapter; FAKE/SGP/ContaAzul plugam pela mesma porta

## Pré-requisitos

- macOS / Linux
- Docker + Docker Compose v2
- (Opcional, pro modo hybrid) [`uv`](https://docs.astral.sh/uv/) (`brew install uv`)
- (Opcional, pro modo hybrid) Python 3.12 (gerenciado pelo `uv` automaticamente)

## Setup local — 3 modos

### Modo 1 — Full Docker (mais simples, recomendado pra subir e ver funcionando)

```bash
git clone git@github.com:PauloPeres/dashboard_velus.git
cd dashboard_velus

cp .env.example .env
# Edite .env e gere FERNET_KEY:
docker run --rm python:3.12-slim python -c \
    "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"

# Sobe tudo (postgres + redis + web + worker + beat):
docker compose up -d

# Primeira execução: aplicar migrations + criar org
docker compose exec web python manage.py migrate
docker compose exec web python manage.py create_organization velus \
    --name="Velus" --owner-email=p.peresjr@gmail.com
docker compose exec web python manage.py createsuperuser
docker compose exec web python manage.py seed_demo_data velus --customers=500

# Abra http://localhost:8000/
```

### Modo 2 — Hybrid dev (mais rápido pra iterar código)

Postgres + Redis em container, Django + Celery no host.

```bash
cp .env.example .env
# Edite .env (gere FERNET_KEY igual ao modo 1)

uv sync
docker compose up -d postgres redis     # só infra

uv run python manage.py migrate
uv run python manage.py create_organization velus --name="Velus" --owner-email=...
uv run python manage.py createsuperuser
uv run python manage.py seed_demo_data velus --customers=500

# Em 3 abas separadas:
uv run python manage.py runserver
uv run celery -A config worker --loglevel=info -Q tenant_velus,celery
uv run celery -A config beat --loglevel=info
```

### Modo 3 — Production-like (futuro, com gunicorn)

Build da imagem com `INSTALL_DEV=false`, gunicorn no comando do web. Cobertura completa virá com o Helm chart na próxima fase de infra.

## Disparar um sync

Três formas, em ordem de preferência:

### 1. UI (recomendado pro dia-a-dia)

Acesse **http://localhost:8000/sync/** — tabela com todas as fontes configuradas por capability, botões `Incremental` / `Bootstrap` por linha, ou `Sincronizar tudo` no topo. Auto-refresh a cada 3s enquanto há job rodando.

**Pré-requisito**: worker rodando. No Modo 1 (Full Docker) já tá rodando. No Modo 2, suba a aba do `celery worker`.

### 2. Celery Beat (automático, a cada 3h)

Schedule `CELERY_BEAT_SCHEDULE` agendado em `config/settings/base.py`:
- `apps.sync.tasks.dispatch_incremental_for_all_orgs` a cada 3h
- Para cada org ativa, enfileira `sync_capability(org, capability, mode=INCREMENTAL)` na fila do tenant
- Suporta múltiplos workers escalando horizontal em prod

### 3. Shell (debug / one-off)

```bash
# Sync síncrono (não usa Celery — útil pra debug):
docker compose exec web python manage.py shell -c "
from apps.sync.tasks import sync_capability
from apps.tenancy.models import Organization
org = Organization.objects.get(slug='velus')
result = sync_capability(organization_id=org.pk, capability='CUSTOMERS', mode='BOOTSTRAP')
print(result)
"

# Sync assíncrono (dispatcha pro worker):
docker compose exec web python manage.py shell -c "
from apps.sync.tasks import sync_capability
from apps.tenancy.models import Organization
org = Organization.objects.get(slug='velus')
task = sync_capability.apply_async(
    kwargs={'organization_id': org.pk, 'capability': 'CUSTOMERS', 'mode': 'BOOTSTRAP'},
    queue=org.celery_queue_name,
)
print(f'Task {task.id} dispatched. Acompanhe em /sync/')
"
```

## Configurar IXC real (quando tiver credenciais)

```bash
docker compose exec web python manage.py shell -c "
from apps.tenancy.models import Organization, OrganizationDataSource
from apps.integrations.shared.enums import SourceType, Capability
org = Organization.objects.get(slug='velus')
for cap in (Capability.CUSTOMERS, Capability.CONTRACTS, Capability.INVOICES):
    ds, _ = OrganizationDataSource.objects.get_or_create(
        organization=org, source_type=SourceType.IXC.value,
        capability=cap.value,
        defaults={'priority': 100, 'is_active': True},
    )
    ds.set_credentials({
        'base_url': 'https://erp.velus.com.br',
        'user_id': 'SEU_USER_ID',
        'api_token': 'SEU_TOKEN',
    })
    ds.save()
"
```

Depois abra `/sync/` e clique **Bootstrap** em cada capability. Sync_completed signal vai automaticamente reconstruir as fact tables e os dashboards refletem dados reais.

## Testes

```bash
# No Modo 1 (Docker):
docker compose exec web pytest

# No Modo 2 (host):
uv run pytest                       # tudo (48 testes, ~2s)
uv run pytest -m "not integration"  # exclui testes lentos
uv run pytest -m e2e                # só end-to-end com FakeSource
uv run pytest --cov                 # com cobertura
```

**Cobertura atual (48 testes):**
- 4 testes de tenant isolation (TenantManager + decorator + cross-context)
- 5 testes e2e de sync customers
- 5 testes e2e de sync financeiro (Contract + Invoice + Payment)
- 2 testes de criptografia Fernet (roundtrip + bytes encriptados no DB)
- 10 testes de value objects (Money + Percentage)
- 14 testes do adapter IXC (Pydantic schema + paginação + Anti-Corruption Layer)
- 8 testes dos simuladores (CLT cost, PJ vs CLT, Simples Anexo III, split)

## Qualidade de código

```bash
uv run ruff check apps/ config/ tests/    # lint
uv run ruff format apps/ config/ tests/   # format
uv run mypy apps/                          # type check
```

## Arquitetura

Resumo (detalhes completos em [`AGENT.md`](AGENT.md)):

```
apps/
├── shared/         ✅ kernel: TenantModel, TenantManager (contextvar), middleware,
│                      EncryptedTextField (Fernet), Money/Percentage VOs,
│                      allow_cross_tenant decorator + audit log
├── tenancy/        ✅ Organization, User custom (email-auth), Membership (RBAC),
│                      OrganizationDataSource (encrypted creds), admin,
│                      create_organization command
├── customers/      ✅ Customer + Contract (DTO/Port/Repository/Model),
│                      seed_demo_data command
├── financial/      ✅ Invoice + Payment (DTO/Port/Repository/Model)
├── analytics/      ✅ dim_* (SCD2) / fact_* tables, aggregations
│                      (MRR/churn/ARPU/aging), signal listener auto-rebuild
├── scenarios/      ✅ 4 simuladores + 27 Assumptions default editáveis
│                      (PJ vs CLT, Salary, Union ESP, Simples Split)
├── sync/           ✅ SyncJob, SyncCheckpoint, dispatch por capability,
│                      UI /sync/ + Beat schedule
├── dashboards/     ✅ 3 telas Plotly (Executive, Revenue, Financial)
├── integrations/
│   ├── shared/     ✅ BaseHttpAdapter (retry/throttle/auth), SourceRegistry, enums
│   ├── ixc/        ✅ IxcCustomerSource + IxcContractSource + IxcInvoiceSource
│   │                  + Pydantic schemas (Anti-Corruption Layer)
│   ├── fake/       ✅ in-memory pra todas as 4 capabilities
│   ├── sgp/        ⏳ placeholder
│   └── contaazul/  ⏳ placeholder
```

**Princípio:** bounded contexts de negócio (`customers`, `financial`, `analytics`, `scenarios`) **nunca importam adapters** — conversam por ports definidos no próprio domain, resolvidos pelo `SourceRegistry`. Guardrail no `ruff` previne esse import.

## URLs principais

| Rota | Conteúdo |
|---|---|
| `/` | Redirect para `/executive/` |
| `/executive/` | Dashboard executivo (MRR + churn + inadimplência + cards) |
| `/revenue/` | Receita & crescimento (MRR + pipeline + ARPU + cohort) |
| `/financial/` | Financeiro & inadimplência (aging + top 50 inadimplentes) |
| `/scenarios/pj-vs-clt/` | Simulador PJ vs CLT |
| `/scenarios/salary-adjust/` | Simulador ajuste salarial |
| `/scenarios/union-esp/` | Simulador sindicato ESP |
| `/scenarios/simples-split/` | Simulador split CNPJ Simples |
| `/scenarios/compare/` | Comparar cenários salvos |
| `/sync/` | Operação de sync (status + botões dispatch) |
| `/admin/` | Django admin (premissas, datasources, syncjobs, audit) |
| `/accounts/login/` | Login Google OAuth |

## Operação

- **Sync incremental automático:** Celery Beat dispatcha a cada 3h por (org, capability)
- **Bootstrap inicial:** via UI `/sync/` clicando "Bootstrap" — pode demorar várias horas em IXC com muitos clientes
- **Monitoramento:** `/sync/` com auto-refresh HTMX. Detalhes históricos em `/admin/sync/`
- **Filas Celery por tenant** (`tenant_<slug>`) — evita noisy-neighbor em multi-tenant

## Segurança

- Credenciais externas (IXC, etc.) criptografadas no DB com **Fernet** — chave em env var separada (em produção, K8s Secret independente do Postgres password)
- **Tenant isolation** via `TenantManager` — todo query filtra por `organization_id` automático. Bypass apenas via `@allow_cross_tenant` com audit log
- Google OAuth com allowlist por `OrganizationMembership`
- CSP estrito em prod (relaxado em dev pra CDNs); HSTS, CSRF, `django-axes` pra rate limit login
- `django-simple-history` em models sensíveis (Org, Membership, DataSource, Customer, Contract, Scenario)

## Licença

Proprietária — uso interno Velus. Contato: [p.peresjr@gmail.com](mailto:p.peresjr@gmail.com)
