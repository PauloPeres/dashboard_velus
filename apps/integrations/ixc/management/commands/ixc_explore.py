"""Explora endpoints do IXC com output anonimizado — seguro pra colar no chat.

Uso:
    docker compose exec web python manage.py ixc_explore <org_slug> --endpoint=<nome> [--limit=3]

Exemplos:
    python manage.py ixc_explore velus --endpoint=cliente --limit=3
    python manage.py ixc_explore velus --endpoint=contrato --limit=5
    python manage.py ixc_explore velus --endpoint=fn --limit=5            # faturas
    python manage.py ixc_explore velus --endpoint=fn_lan --limit=5        # contas a pagar
    python manage.py ixc_explore velus --endpoint=plano_de_contas --limit=20

Output:
- Lista campos retornados (chaves do dict) + tipos detectados
- 1 registro completo de exemplo com VALORES ANONIMIZADOS automaticamente:
  - Nomes, emails, CPFs, CNPJs, tokens → "[REDACTED]" ou "***"
  - IDs e datas preservados pra análise estrutural
- Total de registros, paginação info

Use o output pra ajustar Pydantic schemas em apps/integrations/ixc/schemas.py.
"""

from __future__ import annotations

import json
import re
from typing import Any

from django.core.management.base import BaseCommand, CommandError, CommandParser

from apps.integrations.ixc.client import IxcHttpClient
from apps.integrations.shared.enums import Capability, SourceType
from apps.shared.context import set_current_organization
from apps.shared.decorators import allow_cross_tenant
from apps.tenancy.models import Organization, OrganizationDataSource

# Patterns que detectam PII a anonimizar (preservando estrutura)
_PII_PATTERNS = {
    "cpf": re.compile(r"\d{3}[\.\-]?\d{3}[\.\-]?\d{3}[\-]?\d{2}"),
    "cnpj": re.compile(r"\d{2}[\.\-]?\d{3}[\.\-]?\d{3}[\/]?\d{4}[\-]?\d{2}"),
    "email": re.compile(r"\b[\w.+-]+@[\w-]+\.[\w.-]+\b"),
    "phone": re.compile(r"\b\d{10,11}\b"),
}

# Campos com nomes que sugerem PII — sobrescreve antes de regex
_PII_FIELD_PREFIXES = (
    "nome", "razao", "fantasia", "cpf", "cnpj", "email", "telefone",
    "celular", "endereco", "rua", "bairro", "complemento", "responsavel",
    "rg", "ie", "im", "contato", "observa",
)

# Prefixos de campos monetários — nunca contêm PII, nunca anonimizar
_MONETARY_FIELD_PREFIXES = (
    "valor", "preco", "mensalidade", "comissao", "desconto",
    "juros", "multa", "saldo", "credito", "debito",
)

_MONETARY_VALUE_RE = re.compile(r"\d+[.,]\d{2}")


def _anonymize_value(value: Any, field_name: str) -> Any:
    """Substitui valores PII por placeholders, mantendo estrutura."""
    if value is None or value == "":
        return value
    if isinstance(value, (int, float, bool)):
        return value

    s = str(value)
    field_lower = field_name.lower()

    # Campo monetário → nunca é PII, preserva sem anonimizar
    for prefix in _MONETARY_FIELD_PREFIXES:
        if field_lower.startswith(prefix):
            return s

    # Valor que parece montante monetário → não é PII
    if _MONETARY_VALUE_RE.fullmatch(s.strip()):
        return s

    # Campo com nome suspeito → substitui inteiro
    for prefix in _PII_FIELD_PREFIXES:
        if field_lower.startswith(prefix):
            return f"[REDACTED-{prefix}]"

    # Detecta patterns no valor
    for label, pattern in _PII_PATTERNS.items():
        if pattern.search(s):
            return f"[REDACTED-{label}]"

    # String longa (provável free-text) — trunca
    if len(s) > 80:
        return s[:40] + "…(truncated)"
    return s


def _anonymize_record(record: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for k, v in record.items():
        if isinstance(v, dict):
            out[k] = _anonymize_record(v)
        elif isinstance(v, list):
            out[k] = [_anonymize_record(x) if isinstance(x, dict) else _anonymize_value(x, k) for x in v]
        else:
            out[k] = _anonymize_value(v, k)
    return out


def _summarize_field_types(records: list[dict[str, Any]]) -> dict[str, str]:
    types: dict[str, set[str]] = {}
    for rec in records:
        for k, v in rec.items():
            types.setdefault(k, set()).add(type(v).__name__)
    return {k: " | ".join(sorted(s)) for k, s in types.items()}


class Command(BaseCommand):
    help = "Explora endpoints do IXC mostrando schema + sample anonimizado."

    def add_arguments(self, parser: CommandParser) -> None:
        parser.add_argument("org_slug", type=str)
        parser.add_argument(
            "--endpoint", required=True, type=str,
            help="Nome do endpoint IXC (ex: cliente, contrato, fn, fn_lan, plano_de_contas)",
        )
        parser.add_argument("--limit", type=int, default=3)
        parser.add_argument(
            "--no-anonymize", action="store_true",
            help="NÃO anonimiza (use só em terminal local, NUNCA cole output no chat).",
        )
        parser.add_argument(
            "--filter-qtype", type=str, default="",
            help="Opcional: qtype pra filtro (ex: cliente.id)",
        )
        parser.add_argument(
            "--filter-query", type=str, default="",
            help="Opcional: valor do filtro (ex: 100)",
        )

    @allow_cross_tenant(reason="ixc_explore opera fora de request HTTP")
    def handle(self, *args: Any, **opts: Any) -> None:  # noqa: ARG002
        org_slug: str = opts["org_slug"]
        endpoint: str = opts["endpoint"]
        limit: int = opts["limit"]
        anonymize: bool = not opts["no_anonymize"]

        try:
            org = Organization.objects.get(slug=org_slug)
        except Organization.DoesNotExist as exc:
            raise CommandError(f"Organization '{org_slug}' não existe.") from exc

        set_current_organization(org)

        # Pega credenciais IXC (CUSTOMERS por convenção — todas têm a mesma cred)
        ds = (
            OrganizationDataSource.objects
            .filter(
                organization=org,
                source_type=SourceType.IXC.value,
                capability=Capability.CUSTOMERS.value,
                is_active=True,
            )
            .first()
        )
        if ds is None:
            raise CommandError(
                f"Org '{org_slug}' não tem credenciais IXC configuradas. "
                "Rode `setup_ixc_credentials {org_slug}` primeiro."
            )

        creds = ds.get_credentials()
        self.stdout.write(self.style.SUCCESS(f"\nExplorando IXC: {endpoint}"))
        self.stdout.write(f"  Base URL: {creds['base_url']}")
        self.stdout.write(f"  Limit:    {limit}")
        self.stdout.write(f"  Anon:     {anonymize}\n")

        body_filter = None
        if opts["filter_qtype"] and opts["filter_query"]:
            body_filter = {
                "qtype": opts["filter_qtype"],
                "query": opts["filter_query"],
                "oper": "=",
            }

        with IxcHttpClient(
            base_url=creds["base_url"],
            user_id=creds["user_id"],
            api_token=creds["api_token"],
        ) as client:
            records: list[dict[str, Any]] = []
            try:
                for raw in client.paginate_ixc(
                    endpoint, body_filter=body_filter, page_size=limit
                ):
                    records.append(raw)
                    if len(records) >= limit:
                        break
            except Exception as exc:
                raise CommandError(f"Falha na chamada IXC: {type(exc).__name__}: {exc}") from exc

        if not records:
            self.stdout.write(self.style.WARNING(
                "Nenhum registro retornado. Endpoint vazio ou filtro restritivo demais."
            ))
            return

        # ---------- Output ----------
        self.stdout.write(self.style.SUCCESS(f"\n=== Recebidos {len(records)} registros ==="))

        types = _summarize_field_types(records)
        self.stdout.write(f"\n--- Campos disponíveis ({len(types)}) ---")
        for field, type_info in sorted(types.items()):
            self.stdout.write(f"  {field:30s}  {type_info}")

        self.stdout.write("\n--- Primeiro registro (sample) ---")
        sample = records[0]
        if anonymize:
            sample = _anonymize_record(sample)
        self.stdout.write(json.dumps(sample, indent=2, ensure_ascii=False, default=str))

        if anonymize:
            self.stdout.write(self.style.WARNING(
                "\n⚠ Output anonimizado — seguro pra colar no chat.\n"
                "  Pra ver dados crus localmente: --no-anonymize (NUNCA cole no chat)."
            ))
