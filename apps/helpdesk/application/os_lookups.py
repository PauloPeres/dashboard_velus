"""Resolução de IDs opacos das OS (assunto, técnico) em nomes legíveis.

Os dashboards de OS importam `load_os_lookups(org)` e usam `.subject_name(id)` /
`.technician_name(id)`. A fonte é o `OsLookupCache`, populado por
`python manage.py sync_os_lookups <org_slug>`. Se a org ainda não foi
sincronizada, os resolvers caem num fallback legível ("Assunto #X" / "Técnico #Y").
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from apps.tenancy.models import Organization


@dataclass(frozen=True)
class OsLookups:
    """Resolvers de assunto/técnico carregados uma vez por uso."""

    subject_map: dict[str, str]
    technician_map: dict[str, str]

    def subject_name(self, subject_id: str | None) -> str:
        sid = str(subject_id or "").strip()
        if not sid:
            return "(Sem assunto)"
        return self.subject_map.get(sid) or f"Assunto #{sid}"

    def technician_name(self, technician_id: str | None) -> str:
        tid = str(technician_id or "").strip()
        if not tid:
            return "(Sem técnico)"
        return self.technician_map.get(tid) or f"Técnico #{tid}"


def load_os_lookups(organization: Organization) -> OsLookups:
    """Carrega os mapas de assunto/técnico do OsLookupCache da org.

    Retorna mapas vazios (fallback gracioso) se a org ainda não tiver sido
    sincronizada via `sync_os_lookups`.
    """
    try:
        from apps.helpdesk.infrastructure.models import OsLookupCache

        cache = OsLookupCache.objects.get(organization=organization)
        return OsLookups(
            subject_map=cache.subject_map or {},
            technician_map=cache.technician_map or {},
        )
    except Exception:
        return OsLookups(subject_map={}, technician_map={})
