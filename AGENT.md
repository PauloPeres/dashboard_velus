# AGENT.md — Velus Dashboard

> Regras de desenvolvimento que **toda sessão** trabalhando neste projeto deve seguir.
> Quando uma regra aqui conflitar com a realidade do código, **atualize este documento** — não deixe drift silencioso.

## 0. Antes de qualquer coisa

1. **Leia o `MEMORY.md`** em `/Users/paulo/.claude/projects/-Users-paulo-repos-velus-dashboard-velus/memory/`. Lá estão as decisões arquiteturais já tomadas (stack, tenancy, sync). Não as reabra sem necessidade.
2. **Não invente regras de negócio.** Se uma premissa financeira (encargos CLT, alíquota Simples, taxa de churn esperada) não está explícita, pergunte ao Paulo.
3. **Não introduza dependência nova sem justificar** — esse projeto tem stack fechada (Django + HTMX + Plotly + Postgres + Celery). Toda lib nova precisa de motivo escrito no PR.

---

## 1. Arquitetura — Domain-Driven Design

### 1.1 Bounded Contexts (= um Django app por contexto)

```
apps/
├── shared/         # kernel comum: TenantModel, mixins, encrypted fields, audit, Money/Percentage
├── tenancy/        # Organization, User, OrganizationMembership, OrganizationDataSource
├── customers/      # Customer, Contract, Plan + domain/ports.py (CustomerSourcePort, ContractSourcePort)
├── financial/      # Invoice, Payment, Delinquency + domain/ports.py (InvoiceSourcePort, PaymentSourcePort)
├── analytics/      # fact_* / dim_*, agregações de MRR/churn/ARPU/cohort
├── scenarios/      # Scenario, Assumption, ComparisonResult (simuladores)
├── sync/           # SyncJob, SyncCheckpoint, orquestração — NÃO conhece IXC, conversa por ports
├── integrations/   # adapters concretos (ver §1.6) — IXC, SGP, ContaAzul, Fake, CSV upload
│   ├── shared/     # base HTTP client (retry/paginação/rate-limit), SourceRegistry, Capability enum
│   ├── ixc/
│   ├── sgp/        # placeholder p/ futuro
│   ├── contaazul/  # placeholder p/ futuro
│   └── fake/       # adapter em memória pra testes
└── dashboards/     # views de gráficos, templates, exports PDF
```

**Regra**: um app não importa diretamente models de outro. Comunicação via **serviços de aplicação** (`application/services.py`) ou **eventos de domínio**. Exceção pragmática: `shared/` é importado por todos; `tenancy.Organization` é referenciado por todo `TenantModel`.

**Regra adicional crítica:** bounded contexts de **negócio** (`customers`, `financial`, `analytics`, `scenarios`) **NUNCA importam de `apps/integrations/*`**. A comunicação acontece via ports definidos no domain do próprio contexto e resolvidos pelo `SourceRegistry`. Detalhes em §1.6.

### 1.2 Camadas dentro de cada app (quando o contexto justifica)

```
apps/scenarios/
├── domain/          # Entities, Value Objects, Domain Services — Python puro, sem Django
│   ├── entities.py      # Scenario, Assumption
│   ├── value_objects.py # Money, Percentage, MonthRange
│   └── services.py      # calculate_pj_vs_clt_breakeven(...)
├── application/     # Use Cases — orquestra domain + infra
│   └── use_cases.py     # CreateScenario, CompareScenarios
├── infrastructure/  # Django ORM, repositories, IXC adapter
│   ├── models.py
│   └── repositories.py
└── presentation/    # views, forms, urls, templates
    ├── views.py
    ├── forms.py
    └── urls.py
```

**Quando aplicar camadas completas:** apps com lógica de negócio densa — `scenarios`, `analytics`, `sync`, `financial`.
**Quando colapsar:** apps finos (CRUD quase puro) — `tenancy`, `dashboards`. Forçar 4 camadas em CRUD trivial é cargo culting. Pragmatismo > pureza.

### 1.3 Ubiquitous Language

Código em **inglês**, UI/templates em **português**. Glossário:

| Português (UI/negócio)     | Inglês (código)        |
|----------------------------|------------------------|
| Cliente                    | Customer               |
| Contrato                   | Contract               |
| Fatura / Boleto            | Invoice                |
| Inadimplência              | Delinquency            |
| Pagamento / Recebimento    | Payment                |
| Plano                      | Plan                   |
| Organização (tenant)       | Organization           |
| Cenário (simulador)        | Scenario               |
| Premissa                   | Assumption             |
| Receita Recorrente Mensal  | MonthlyRecurringRevenue (MRR) |
| Cancelamento / Churn       | Churn                  |

Quando aparecer um termo novo de negócio, **adicione aqui antes de codar**.

### 1.4 Agregados

- **Organization** — aggregate root da tenancy. Quase tudo nasce dela.
- **Contract** — aggregate root do contexto financeiro (Invoice e Payment dependem dele).
- **Scenario** — aggregate root do contexto de simulação (Assumption e ComparisonResult internos).

**Regra:** mutação de entidades dentro de um agregado **só passa pelo root**. Não escreva direto em `Invoice` sem ir pelo `Contract` correspondente.

### 1.5 Eventos de domínio (sem over-engineering)

Use Django signals para eventos críticos que reduzem acoplamento:
- `sync_completed` → recomputa fact tables agregadas, invalida cache de dashboard
- `scenario_saved` → snapshot de premissas pro audit log
- `organization_created` → kick off bootstrap inicial

**Não** monte event bus genérico. Quando sinal não couber, é hora de extrair — não antes.

### 1.6 Camada de integração externa — Ports & Adapters (Hexagonal)

**Princípio central:** o domínio define **o que precisa**, não **de quem vem**. IXC hoje, ContaAzul amanhã, SGP depois — o domain não pode saber.

#### Estrutura

```python
# apps/customers/domain/dto.py
@dataclass(frozen=True)
class CustomerDTO:
    """Representação neutra de cliente. Não tem campo source-specific."""
    external_id: str           # ID no sistema de origem (string, opaco)
    document: str              # CPF/CNPJ — base p/ resolução de identidade entre fontes
    name: str
    email: str | None
    phone: str | None
    created_at: datetime
    raw_extras: dict[str, Any] = field(default_factory=dict)  # campos source-specific opacos

# apps/customers/domain/ports.py
class CustomerSourcePort(Protocol):
    def list_customers(self, *, since: datetime | None = None) -> Iterable[CustomerDTO]: ...
    def get_customer(self, external_id: str) -> CustomerDTO | None: ...

# apps/integrations/ixc/customers.py
class IxcCustomerSource:
    """Implementa CustomerSourcePort traduzindo schema IXC → DTO neutro."""
    capabilities = {Capability.CUSTOMERS}

    def list_customers(self, *, since=None):
        for raw in self._client.paginate("clientes", filters=self._since_filter(since)):
            validated = IxcCustomerSchema.model_validate(raw)  # Anti-Corruption Layer
            yield CustomerDTO(
                external_id=str(validated.id),
                document=validated.cnpj_cpf,
                name=validated.razao,
                ...
            )
```

#### Princípios (oito, todos obrigatórios)

1. **Domain não importa integrations.** Só importa seus próprios ports e DTOs. Quem faz o wiring é a camada de aplicação ou o `apps/sync/`.

2. **DTOs neutros.** Campos comuns explícitos; specifics vão em `raw_extras: dict` (opaco, sem acessar do domain). Quando um campo vira essencial pra >1 fonte, promove pro DTO.

3. **Anti-Corruption Layer obrigatória.** Adapter SEMPRE valida resposta externa com **Pydantic** antes de virar DTO. Mudança de schema do IXC fica contida no adapter — domain não sente.

4. **Identidade composta na persistência.** Models de domínio têm `unique_together = ('organization', 'source_type', 'external_id')`. Mesmo cliente vindo de 2 fontes = 2 registros de origem; identidade lógica (mesmo cliente físico) é resolvida por `document` (CPF/CNPJ) num serviço de domínio explícito (`customers.domain.services.resolve_identity()`).

5. **Registry por Organization.** Modelo `tenancy.OrganizationDataSource(organization, source_type, capability, credentials_encrypted, priority, is_active)`. Uma org pode ter:
   - IXC para customers + contracts + invoices
   - ContaAzul para invoices (priority maior — sobrescreve IXC)
   - CSV upload para customers ad-hoc
   `SourceRegistry.get_sources(org, Capability.INVOICES)` devolve a lista ordenada por prioridade.

6. **Capabilities explícitas.** Cada adapter declara o que implementa via atributo de classe `capabilities: set[Capability]`. Registry filtra; tentar usar adapter sem capability levanta erro.

7. **Resolução de conflito multi-fonte.** Quando 2 fontes têm a mesma entidade (match por documento):
   - **Prioridade simples**: vence a fonte com maior `priority` no `OrganizationDataSource`.
   - **Merge por campo**: quando explicitamente decidido, em `<context>.domain.services.merge_<entity>()` (ex.: dados cadastrais do IXC + status financeiro do ContaAzul). Decisão de merge é **escrita no código + comentário**, nunca implícita.

8. **`FakeSource` em `apps/integrations/fake/`.** Implementa todos os ports com dados em memória. **Testes de application/dashboard/scenarios nunca tocam IXC real.** Default em settings de teste: registry registra `Fake*` adapters.

#### Adicionar nova integração — checklist

1. ☐ Criar `apps/integrations/<nome>/`
2. ☐ Herdar do base HTTP client em `apps/integrations/shared/http_client.py` (paginação, retry, rate-limit já prontos)
3. ☐ Pydantic schemas dos endpoints em `schemas.py` (1 schema = 1 endpoint, sem hierarquia barroca)
4. ☐ Implementar cada port suportado em arquivo próprio (`customers.py`, `financial.py`, ...)
5. ☐ Declarar `capabilities` no adapter
6. ☐ Adicionar valor no `Capability` / `SourceType` enums (`apps/integrations/shared/enums.py`)
7. ☐ Registrar no `SourceRegistry` (auto-discovery via `apps.py.ready()`)
8. ☐ Comando admin pra configurar credenciais por org: `setup_data_source <org-slug> <source-type> --capability=...`
9. ☐ Testes de Pydantic validation contra fixture real de cada endpoint
10. ☐ Documentar quirks/rate-limit observados em `apps/integrations/<nome>/README.md`

#### Proibido

- ❌ `from apps.integrations.ixc...` dentro de `apps/customers/`, `apps/financial/`, `apps/analytics/`, `apps/scenarios/`
- ❌ Vazar campo source-specific (`id_ixc`, `cliente_id_ixc`) pra dentro do domain
- ❌ Hardcodar `if source_type == "IXC"` em código de domínio — use registry/ports
- ❌ Bypass do Pydantic ("vou só pegar esse campo do dict cru, é mais rápido") — Anti-Corruption Layer não é opcional

### 1.7 Sync — orquestração, não adapter

`apps/sync/` é só **orquestração**. Não conhece IXC nem nenhum sistema externo.

```python
# apps/sync/tasks.py
@shared_task(bind=True, autoretry_for=(...), retry_backoff=True)
def sync_capability(self, organization_id: int, capability: str, mode: Literal["bootstrap", "incremental"]):
    org = Organization.objects.get(id=organization_id)
    set_current_organization(org)

    for source in SourceRegistry.get_sources(org, Capability(capability)):
        checkpoint = SyncCheckpoint.objects.get_or_create(org=org, source=source.source_type, capability=capability)
        since = None if mode == "bootstrap" else checkpoint.last_processed_at

        for dto in source.list_for(capability, since=since):
            repository_for(capability).upsert_from_dto(dto, source_type=source.source_type)

        checkpoint.last_processed_at = timezone.now()
        checkpoint.save()

    sync_completed.send(sender=None, org=org, capability=capability)
```

- **`SyncJob`** — registro de execução por `(org, source_type, capability)`
- **`SyncCheckpoint`** — cursor/último-processado por `(org, source_type, capability)` — bootstrap reaproveita p/ retomar
- **Filas Celery por tenant** (`tenant_<slug>`) — orquestração por org isolada
- **Dispatch por capability**, não por source — sync code não precisa mudar quando adapter novo entra

---

## 2. DRY — pragmático, não dogmático

### 2.1 O que DEVE ser compartilhado (em `apps/shared/`)

- `TenantModel` (abstrato) + `TenantManager`
- Mixins: `TimestampedMixin` (`created_at`, `updated_at`), `AuditedMixin` (`created_by`, `updated_by`)
- `EncryptedField` (Fernet)
- Base do cliente HTTP IXC (paginação, retry, rate limit) — subclasses por endpoint
- Value Objects financeiros: `Money` (Decimal + currency), `Percentage`, `DateRange`
- Helpers de template comuns (formatação BR de moeda, datas, CPF/CNPJ mask)

### 2.2 O que NÃO DEVE ser compartilhado

- **Models de domínio de bounded contexts diferentes.** O `Customer` em `customers/` e o `dim_customer` em `analytics/` **não são o mesmo model** — eles refletem realidades diferentes (transacional vs analítico). Não unifique.
- Lógica que parece igual em 2 lugares mas vai divergir (premissas de simulador vs cálculo de MRR analytics).

### 2.3 Regra das 3 ocorrências

**Não extraia abstração antes da 3ª duplicação.** Abstração prematura é pior que copy-paste — fica errada e amarra todo o código nela. Se aparece a 3ª vez, aí extrai.

### 2.4 Adapters de integração (IXC, futuras)

- **Base HTTP client em `apps/integrations/shared/http_client.py`** — paginação, retry exponencial, rate-limit, auth abstrata. Todo adapter herda dele.
- **1 arquivo por capability dentro do adapter** (`ixc/customers.py`, `ixc/financial.py`) — não 1 arquivo gigante.
- **1 schema Pydantic por endpoint**, sem hierarquia barroca de herança. Schemas vivem em `<adapter>/schemas.py`.
- **Nunca** duplique paginação/retry/rate-limit nas subclasses — sobe pro base. Se aparece particularidade (ex.: cursor estilo diferente), abstrai com método template (`_extract_next_cursor`).
- **`SourceRegistry` em `apps/integrations/shared/registry.py`** é singleton — auto-discovery dos adapters via `AppConfig.ready()` de cada `apps/integrations/<nome>/`.

---

## 3. Segurança

### 3.1 Autenticação

- **Google OAuth via `django-allauth`** (battle-tested). Não rolar autenticação própria.
- **Allowlist por Organization** via `OrganizationMembership(user, organization, role)`. Login sem membership ativo → 403.
- Sem signup público no MVP. Tenants criados via `manage.py create_organization`.
- **Cookies de sessão**:
  ```python
  SESSION_COOKIE_SECURE = True
  SESSION_COOKIE_HTTPONLY = True
  SESSION_COOKIE_SAMESITE = 'Lax'
  SESSION_COOKIE_AGE = 60 * 60 * 24  # 1 dia
  SESSION_EXPIRE_AT_BROWSER_CLOSE = True
  ```
- **Rotação de sessão** em todo login bem-sucedido (`request.session.cycle_key()`).
- **`django-axes`** pra rate-limit de tentativas de login (5 tentativas / 15 min / IP).

### 3.2 Secrets

- **Tudo via env var, lido de K8s Secret.** Nada de secret em `settings.py` versionado.
- `pydantic-settings` pra config tipada — falha cedo se variável obrigatória faltar.
- **Token IXC**: campo `Organization.ixc_credentials` criptografado com **Fernet**. Key de criptografia em K8s Secret **separado** do Postgres password. Descriptografia só em memória do worker, no momento da chamada.
- **Nunca** logar credenciais. Nunca passar credenciais em URL.
- Rotação de Fernet key documentada em runbook.

### 3.3 Isolamento entre tenants

- `TenantManager` filtra todo query de domínio. Bypass exige decorator explícito `@allow_cross_tenant("razão escrita")` + entrada no audit log.
- **Todo teste de model de domínio cria 2 organizações e prova que uma não vê dados da outra.** Sem exceção.
- Em views: além do TenantManager, **`get_object_or_404(qs, ...)` sempre via queryset escopado**. Nunca `Model.objects.get(pk=...)` em request handler.
- Em tasks Celery: `organization_id` **explícito em kwargs**, setado em `contextvars` antes de qualquer query.

### 3.4 Django hardening (produção)

```python
DEBUG = False
ALLOWED_HOSTS = [...]               # whitelist explícita
SECURE_PROXY_SSL_HEADER = ('HTTP_X_FORWARDED_PROTO', 'https')
SECURE_SSL_REDIRECT = True
SECURE_HSTS_SECONDS = 31536000      # 1 ano
SECURE_HSTS_INCLUDE_SUBDOMAINS = True
SECURE_HSTS_PRELOAD = True
SECURE_CONTENT_TYPE_NOSNIFF = True
SECURE_BROWSER_XSS_FILTER = True
SECURE_REFERRER_POLICY = 'same-origin'
X_FRAME_OPTIONS = 'DENY'
CSRF_COOKIE_SECURE = True
```

- **CSP** via `django-csp` — default deny, libera só o necessário (Plotly CDN se usado, ou self-host JS).
- **CSRF jamais desabilitado.** Endpoints HTMX recebem CSRF via header automaticamente — configurar isso no template base.
- **SQL**: ORM por padrão. Raw SQL só parametrizado (`%s`), nunca f-string com input.

### 3.5 LGPD

- Campos PII (CPF, CNPJ, email do cliente final, telefone) marcados em model Meta (`pii_fields = [...]`).
- **Logs nunca contêm PII.** Use IDs opacos. Adicione middleware que filtra payload de log.
- **Direito ao esquecimento**: função `anonymize_customer(customer_id)` por aggregate — anonimiza, não deleta (pra manter fact tables consistentes).
- **Export de dados**: só da própria org, link assinado com TTL curto (< 1h).
- **Audit log** em `Scenario`, `Organization`, `OrganizationMembership`, mudança de credenciais. Use `django-simple-history` ou implementação própria.

---

## 4. Boas práticas Django

### 4.1 Layout do projeto

```
config/
├── settings/
│   ├── base.py
│   ├── development.py
│   └── production.py
├── urls.py
├── wsgi.py
└── asgi.py
apps/
├── shared/
├── tenancy/
├── customers/
├── financial/
├── analytics/
├── scenarios/
├── sync/
└── dashboards/
manage.py
pyproject.toml
```

### 4.2 Models

- **Sempre** definir `Meta.indexes` para padrões de query (`(organization_id, data_*)` é o mais comum).
- **Sempre** definir `__str__` (sanidade no admin).
- **Sem regra de negócio nos models** além de properties triviais. Lógica vai pra domain services.
- `models.TextChoices` pra enums (não strings soltas).
- `db_index=True` em FKs muito consultadas, mas não em todas — índice tem custo de escrita.
- Toda model de domínio herda de `TenantModel` (ver [tenancy memory](../.claude/projects/-Users-paulo-repos-velus-dashboard-velus/memory/velus-dashboard-tenancy.md)).

### 4.3 Migrations

- **Uma migration por mudança lógica.** Não bundle "ajuste vários models" em 1 só.
- **Migrations de dados separadas** das de schema. Não misture.
- Reversíveis quando possível; quando não, documente o porquê em comentário.
- CI roda `makemigrations --check --dry-run` — falha se há mudança de model não migrada.

### 4.4 Views

- **CBVs** pra CRUD; **FBVs** pra dashboards (composição de dados fica mais legível).
- **Sempre** `LoginRequiredMixin` + verificação de membership ativo da Organization.
- HTMX endpoints retornam **partial templates**, não JSON. (Renderização server-side é o ponto.)
- Cada view valida que o objeto pertence à org do usuário — não confie só no TenantManager (defesa em profundidade).

### 4.5 Forms

- `ModelForm` quando aplicável.
- **Validação server-side é obrigatória.** Client-side é só UX.
- Forms de simulador delegam validação de premissas ao **domain layer** (`apps/scenarios/domain/services.py`).

### 4.6 URLs

- App-prefixadas, **named URLs sempre**, zero hardcoded path em template.
- `path()` por padrão; `re_path()` só se realmente necessário.

### 4.7 Templates

- 1 base layout, composição via `{% block %}`.
- HTMX: `hx-target` e `hx-swap` **sempre explícitos**.
- Plotly: gráfico renderizado server-side como JSON, embebido via `{{ chart_json|json_script:"chart-id" }}`.
- Sem `<style>` inline; sem `<script>` inline (CSP).
- Tailwind: classes utilitárias direto no template; componentes reutilizáveis viram `{% include %}` ou template tags.

### 4.8 Testes

- **`pytest-django`**, não `TestCase`.
- **`factory_boy`** sempre. Zero `.objects.create()` em teste.
- Tiers:
  - **Unit**: domain layer puro (sem DB). Rápido. Cobertura alta.
  - **Integration**: ORM real, IXC adapter mockado.
  - **E2E smoke**: principais dashboards renderizam sem 500.
- **Obrigatório**:
  - Todo model que herda `TenantModel` tem teste de "no cross-tenant leak"
  - Todo endpoint IXC tem teste de validação Pydantic contra fixture de resposta real

### 4.9 Celery

- **Nomes explícitos**: `apps.sync.tasks.bootstrap_customers`
- `organization_id` **sempre** em kwargs. Nunca implícito.
- **Idempotência por padrão**: task pode rerodar sem corromper estado (use cursores/checkpoints).
- **Retry explícito por task** (`autoretry_for`, `retry_backoff`, `max_retries`). Sem retry-forever global.
- Bootstrap longo = `chord` de chunks paginados, não 1 task monolítica.
- Schedules de Beat em código (`CELERY_BEAT_SCHEDULE`). Use `django-celery-beat` (DB) apenas se houver agendamento dinâmico por tenant.
- **Filas por tenant** (`tenant_<slug>`) pra evitar noisy-neighbor.

### 4.10 Configuração

- `pydantic-settings` com classes por ambiente.
- Defaults sensatos pra dev; produção **falha alto** se variável obrigatória faltar.
- `.env.example` versionado, `.env` no `.gitignore`.

### 4.11 Performance

- `select_related`/`prefetch_related` onde há FK acessada em loop.
- **Asserts de contagem de query** em views críticas (`django_assert_num_queries`).
- **Cache de dashboards** em Redis com invalidação via signal `sync_completed`. TTL como fallback (1h), invalidação como primária.
- Agregações pesadas: materialize em fact tables, não compute on-the-fly.

### 4.12 Logging

- **Structured (JSON)** em produção.
- **Nunca logar secret, nunca logar PII.**
- `organization_id` no contexto de todo log de request (middleware injeta).
- Níveis: DEBUG dev only; INFO pra eventos de negócio; WARNING pra recuperáveis; ERROR pra falha real (com Sentry ou equivalente).

---

## 5. Qualidade de código

- **Python 3.12+**
- **Type hints** em toda função pública.
- **`ruff`** pra lint + format (substitui black, flake8, isort).
- **`mypy --strict`** em `apps/*/domain/` (Python puro); modo padrão no resto.
- **Pre-commit hooks**: ruff, mypy nos domains, bloqueio de `print()`/`pdb`/`breakpoint()`.
- **Sem `# noqa`** sem comentário explicando por quê.
- Funções > 50 linhas ou complexity > 10 → refatorar ou justificar em comentário.

---

## 6. Definition of Done por feature

Uma feature está pronta quando:

1. ☐ Testes passam (unit + integration + cross-tenant isolation)
2. ☐ Migrations checked-in e reversíveis (ou documentado por quê não)
3. ☐ Admin registrado se model relevante pra ops
4. ☐ URL nomeada, em `urls.py` do app, registrada no root
5. ☐ Templates herdam base layout, sem styles/scripts inline
6. ☐ Logs em formato estruturado, sem PII
7. ☐ Sem secret novo em código
8. ☐ Models tocados têm `__str__`, `Meta.indexes`, campos de auditoria
9. ☐ Glossário (seção 1.3) atualizado se termo novo apareceu
10. ☐ **Este AGENT.md atualizado** se nova convenção foi introduzida

---

## 7. Quando pedir ajuda ao humano

Pergunte ao Paulo **antes** de:

- Adicionar dependência nova (lib, serviço externo)
- Mudar premissa financeira ou alíquota tributária (esses números vêm dele, não de docs genéricas)
- Decidir cadência de sync diferente do combinado (3h)
- Mudar a regra de tenancy (qualquer coisa que afete `TenantManager`)
- Introduzir signup público / billing / SaaS-isation (escopo deliberadamente fora do MVP)
- Quebrar a regra das 3 ocorrências pra criar abstração
- **Adicionar nova integração externa** (novo ERP, novo sistema contábil) — confirmar quais capabilities ela cobre e regra de prioridade vs adapters existentes
- **Decidir estratégia de merge** quando 2 fontes têm a mesma entidade (CPF/CNPJ batendo entre IXC e outra fonte) — não inventar regra, perguntar
- **Promover campo de `raw_extras` pro DTO neutro** — significa que o domain vai depender dele; confirmar que é genuíno cross-fonte

Quando em dúvida sobre **negócio**, pergunte. Quando em dúvida sobre **convenção técnica**, leia este doc; se não está aqui, decida e **atualize este doc**.
