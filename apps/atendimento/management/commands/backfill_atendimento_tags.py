"""Backfill de `Atendimento.tags` a partir do `raw_extras` + catalogo de etiquetas.

Os ids das tags (`id_tag`) ja estao gravados no `raw_extras` de todos os
atendimentos ja sincronizados (a Opa! sempre mandou o campo `tags` na listagem,
e o schema o preserva em raw_extras). Este comando resolve esses ids pra nome
usando o catalogo de Etiqueta e popula o campo estruturado `tags` — sem re-puxar
a API de atendimentos.

Uso:
    python manage.py backfill_atendimento_tags velus              # sync catalogo + backfill
    python manage.py backfill_atendimento_tags velus --skip-catalog-sync
    python manage.py backfill_atendimento_tags velus --dry-run

Idempotente: so grava quando o `tags` resolvido difere do atual.
"""

from __future__ import annotations

from typing import Any

import structlog
from django.core.management.base import BaseCommand, CommandError, CommandParser

from apps.atendimento.infrastructure.models import Atendimento, Etiqueta
from apps.atendimento.infrastructure.repositories import EtiquetaRepository
from apps.integrations.opa.atendimento import OpaAtendimentoSource
from apps.integrations.shared.enums import Capability, SourceType
from apps.shared.context import set_current_organization
from apps.shared.decorators import allow_cross_tenant
from apps.tenancy.models import Organization, OrganizationDataSource

_logger = structlog.get_logger(__name__)


def extract_tag_ids(raw_extras: Any) -> list[str]:
    """Extrai os `id_tag` (dedup, preservando ordem) do `raw_extras['tags']`.

    Espelha `OpaAtendimentoSchema.tag_ids`: cada item de `tags` e um dict
    `{_id, data, id_tag, id_atendente}` — a identidade da etiqueta e o `id_tag`.
    """
    if not isinstance(raw_extras, dict):
        return []
    raw = raw_extras.get("tags")
    if not isinstance(raw, list):
        return []
    out: list[str] = []
    seen: set[str] = set()
    for t in raw:
        if not isinstance(t, dict):
            continue
        tid = t.get("id_tag")
        tid = str(tid) if tid is not None else ""
        if tid and tid not in seen:
            seen.add(tid)
            out.append(tid)
    return out


class Command(BaseCommand):
    help = "Popula Atendimento.tags a partir do raw_extras + catalogo de etiquetas."

    def add_arguments(self, parser: CommandParser) -> None:
        parser.add_argument("org_slug", type=str, help="Slug da organização")
        parser.add_argument(
            "--skip-catalog-sync",
            action="store_true",
            help="Não ressincroniza o catálogo de etiquetas antes do backfill.",
        )
        parser.add_argument(
            "--batch-size",
            type=int,
            default=1000,
            help="Tamanho do lote de bulk_update. Default 1000.",
        )
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Só relata quantos seriam atualizados, sem gravar.",
        )

    @allow_cross_tenant(reason="backfill opera fora de request HTTP")
    def handle(self, *args: Any, **opts: Any) -> None:  # noqa: ARG002
        org_slug = str(opts["org_slug"])
        skip_catalog = bool(opts["skip_catalog_sync"])
        batch_size = int(opts["batch_size"])
        dry_run = bool(opts["dry_run"])

        try:
            org = Organization.objects.get(slug=org_slug)
        except Organization.DoesNotExist as exc:
            raise CommandError(f"Organização '{org_slug}' não existe.") from exc

        set_current_organization(org)

        # --- 1. Sincroniza o catálogo de etiquetas (id_tag -> nome) ----------
        if not skip_catalog:
            ds = OrganizationDataSource.objects.filter(
                organization=org,
                source_type=SourceType.OPA.value,
                capability=Capability.ATENDIMENTO.value,
                is_active=True,
            ).first()
            if not ds:
                raise CommandError(
                    f"Org '{org_slug}' sem credenciais Opa!. Rode "
                    "`setup_opa_credentials` ou use --skip-catalog-sync."
                )
            creds = ds.get_credentials()
            source = OpaAtendimentoSource(base_url=creds["base_url"], token=creds["token"])
            et_repo = EtiquetaRepository(org)
            n_et = 0
            for et in source.list_etiquetas():
                et_repo.upsert_from_dto(et, source_type=SourceType.OPA)
                n_et += 1
            self.stdout.write(f"Catálogo de etiquetas sincronizado: {n_et} etiquetas.")

        # --- 2. Mapa id_tag -> nome a partir do catálogo no banco ------------
        etiqueta_map: dict[str, str] = dict(
            Etiqueta.objects.filter(organization=org, source_type=SourceType.OPA.value)
            .exclude(nome="")
            .values_list("external_id", "nome")
        )
        self.stdout.write(f"Mapa de resolução: {len(etiqueta_map)} etiquetas com nome.")

        # --- 3. Resolve e grava tags atendimento a atendimento ---------------
        qs = Atendimento.objects.filter(organization=org, source_type=SourceType.OPA.value).only(
            "id", "raw_extras", "tags"
        )

        batch: list[Atendimento] = []
        scanned = 0
        with_tags = 0
        updated = 0
        unresolved: set[str] = set()

        for at in qs.iterator(chunk_size=batch_size):
            scanned += 1
            ids = extract_tag_ids(at.raw_extras)
            if ids:
                with_tags += 1
            new_tags = []
            for tid in ids:
                nome = etiqueta_map.get(tid)
                if nome is None:
                    unresolved.add(tid)
                    new_tags.append(tid)  # fallback rastreável
                else:
                    new_tags.append(nome)
            if new_tags != (at.tags or []):
                at.tags = new_tags
                batch.append(at)
            if len(batch) >= batch_size:
                if not dry_run:
                    Atendimento.objects.bulk_update(batch, ["tags"])
                updated += len(batch)
                batch = []

        if batch:
            if not dry_run:
                Atendimento.objects.bulk_update(batch, ["tags"])
            updated += len(batch)

        _logger.info(
            "backfill_atendimento_tags_done",
            org=org.slug,
            scanned=scanned,
            with_tags=with_tags,
            updated=updated,
            unresolved_ids=len(unresolved),
            dry_run=dry_run,
        )
        prefix = "[dry-run] " if dry_run else ""
        self.stdout.write(
            self.style.SUCCESS(
                f"\n✓ {prefix}Backfill tags: {scanned} atendimentos varridos, "
                f"{with_tags} com tags no raw, {updated} atualizados, "
                f"{len(unresolved)} id_tag sem nome no catálogo."
            )
        )
        if unresolved:
            self.stdout.write(
                "id_tag não resolvidos (mantidos como id): " + ", ".join(sorted(unresolved)[:20])
            )
