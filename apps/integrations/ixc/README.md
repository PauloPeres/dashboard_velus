# Adapter IXC Soft

Implementa ports do bounded context `customers` (e futuramente `financial`)
contra a REST API do **IXC Soft** — ERP usado pela Velus.

## Quirks da API conhecidos

| Quirk | Detalhe |
|---|---|
| **GET com body** | Endpoints de listagem (`cliente`, `contrato`, etc.) usam GET mas com body JSON contendo filtros + paginação. Atípico. |
| **Header `ixcsoft: listar`** | Diferencia operação de listagem de outras operações sobre o mesmo recurso. |
| **Auth Basic** | Base64 de `user_id:api_token`. Gerado em Configurações → Usuários do IXC. |
| **Paginação page-based** | Body inclui `page` (1-indexed) e `rp` (registros por página, max ~500 dependendo da instalação). |
| **Response wrapper** | `{"page": "1", "total": "1234", "registros": [...]}`. Note `total` como string. |
| **Tipos inconsistentes** | Mesmo campo pode vir como `int` ou `string` entre versões. Pydantic schema coage com `mode="before"`. |
| **Datas sem timezone** | Formato `YYYY-MM-DD HH:MM:SS`, sem TZ. Assumimos `America/Sao_Paulo` (onde o IXC roda). |
| **`ativo` como `S`/`N`** | Não boolean. Adapter traduz para status canônico (`ACTIVE`/`CANCELED`/`UNKNOWN`). |
| **Rate limit não documentado** | Começamos conservador em 3 req/s. Ajustar conforme observado no bootstrap. |

## Mapeamento `cliente` → `CustomerDTO`

| IXC | DTO |
|---|---|
| `id` | `external_id` |
| `cnpj_cpf` | `document` (normalizado pra só dígitos) |
| `razao` | `name` |
| `email` | `email` |
| `telefone_celular` | `phone` |
| `ativo` | `status` (`S` → `ACTIVE`, `N` → `CANCELED`) |
| `data_cadastro` | `created_at_source` (com TZ Sao_Paulo) |
| outros | `raw_extras` (opaco — `model_extra` do Pydantic) |

## Filtros de incremental

`since: datetime` vira:
```json
{"qtype": "cliente.data_alteracao", "query": "2025-05-28 12:00:00", "oper": ">="}
```

Assume que `data_alteracao` é o campo de cursor confiável. Se a instalação
não suportar, fallback é puxar tudo e filtrar client-side (mais lento).

## Configurar via `OrganizationDataSource`

```bash
python manage.py create_organization velus \
    --name="Velus" \
    --owner-email=p.peresjr@gmail.com \
    --ixc-base-url=https://erp.example.com.br \
    --ixc-user-id=1 \
    --ixc-token=abc123...
```

Credenciais ficam **criptografadas com Fernet** em
`tenancy_organizationdatasource.credentials_encrypted`. Descriptografadas só
em memória do worker no momento da chamada.

## Quando o IXC mudar schema

Sintoma: testes começam a falhar com `AdapterContractError`.
Diagnóstico: log estruturado `ixc_customer_schema_invalid` mostra os campos.
Correção: ajustar `apps/integrations/ixc/schemas.py` + rerodar testes com
fixture atualizada.

**Não corrigir bypassando Pydantic** — Anti-Corruption Layer é deliberado.
