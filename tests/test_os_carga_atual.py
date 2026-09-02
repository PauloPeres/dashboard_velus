"""Testes de `compute_os_carga_atual` — a foto de agora das OS abertas.

As decisões que os testes travam:

- é ESTOQUE: o que conta é o estado atual, não a janela de abertura;
- fila viva e backlog são números separados — somá-los faz a fila parecer
  várias vezes maior que a carga real da equipe;
- SLA vem da agenda (o IXC não preenche os campos de SLA), e OS sem agenda
  marcada fica FORA da base do percentual, não do lado "no prazo";
- pessoa com fila 100% parada é sinalizada, para o ranking por total não virar
  ranking de quem herdou backlog.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from apps.analytics.application.aggregations import compute_os_carga_atual
from apps.helpdesk.infrastructure.models import OsLookupCache, Ticket
from apps.integrations.shared.enums import SourceType
from apps.shared.context import set_current_organization
from apps.tenancy.models import Organization

AGORA = datetime(2026, 9, 2, 12, 0, tzinfo=UTC)


def _os(
    org: Organization,
    external_id: str,
    *,
    status: str = Ticket.Status.SCHEDULED.value,
    dias_aberta: int = 1,
    agenda_em_dias: int | None = None,
    technician_id: str = "t1",
    subject_id: str = "s1",
) -> Ticket:
    return Ticket.objects.create(
        organization=org,
        source_type=SourceType.IXC.value,
        external_id=external_id,
        customer_external_id="c1",
        technician_id=technician_id,
        subject_id=subject_id,
        status=status,
        opened_at=AGORA - timedelta(days=dias_aberta),
        scheduled_at=(
            None if agenda_em_dias is None else AGORA + timedelta(days=agenda_em_dias)
        ),
    )


@pytest.fixture
def cenario_os(db, organization_a: Organization) -> Organization:
    set_current_organization(organization_a)
    OsLookupCache.objects.create(
        organization=organization_a,
        subject_map={"s1": "Manutenção Técnica", "s2": "Retirada de Equipamentos"},
        technician_map={"t1": "Gislaine", "t2": "Rodrigo"},
    )

    # Gislaine: fila viva (2 recentes, 1 delas com agenda vencida) + 1 backlog.
    _os(organization_a, "1", dias_aberta=2, agenda_em_dias=1)
    _os(organization_a, "2", dias_aberta=3, agenda_em_dias=-1)
    _os(organization_a, "3", dias_aberta=200, agenda_em_dias=-100, subject_id="s2")
    # Rodrigo: fila inteiramente parada.
    _os(organization_a, "4", dias_aberta=400, agenda_em_dias=-300, technician_id="t2", subject_id="s2")
    _os(organization_a, "5", dias_aberta=300, agenda_em_dias=-200, technician_id="t2", subject_id="s2")
    # Sem técnico e sem agenda.
    _os(organization_a, "6", dias_aberta=10, agenda_em_dias=None, technician_id="")
    # Fechada: não entra em lugar nenhum.
    _os(organization_a, "7", status=Ticket.Status.CLOSED.value, dias_aberta=1)
    return organization_a


@pytest.mark.django_db
class TestComputeOsCargaAtual:
    def test_counts_only_open_orders(self, cenario_os: Organization) -> None:
        data = compute_os_carga_atual(cenario_os, agora=AGORA)
        assert data["total"] == 6  # a fechada fica de fora

    def test_splits_live_queue_from_stalled_backlog(
        self, cenario_os: Organization
    ) -> None:
        data = compute_os_carga_atual(cenario_os, agora=AGORA)
        assert data["fila_viva"] == 3  # OS 1, 2 e 6
        assert data["backlog"] == 3  # OS 3, 4 e 5
        assert data["pct_backlog"] == 50.0

    def test_sla_from_schedule_excludes_orders_without_one(
        self, cenario_os: Organization
    ) -> None:
        """OS sem agenda não é "no prazo" — é "sem prazo", e sai da base."""
        data = compute_os_carga_atual(cenario_os, agora=AGORA)
        sla = data["sla"]
        assert sla["vencidas"] == 4  # OS 2, 3, 4, 5
        assert sla["no_prazo"] == 1  # OS 1
        assert sla["sem_agenda"] == 1  # OS 6
        # Base = 5 (com agenda), não 6.
        assert sla["pct_vencidas"] == 80.0

    def test_per_person_breakdown_by_age(self, cenario_os: Organization) -> None:
        data = compute_os_carga_atual(cenario_os, agora=AGORA)
        por_pessoa = {p["nome"]: p for p in data["por_pessoa"]}

        gislaine = por_pessoa["Gislaine"]
        assert gislaine["total"] == 3
        assert gislaine["recente"] == 2
        assert gislaine["backlog"] == 1
        assert gislaine["fila_viva"] == 2
        assert gislaine["sla_vencidas"] == 2  # OS 2 e 3

        rodrigo = por_pessoa["Rodrigo"]
        assert rodrigo["total"] == 2
        assert rodrigo["fila_viva"] == 0

    def test_flags_person_with_fully_stalled_queue(
        self, cenario_os: Organization
    ) -> None:
        """Rodrigo tem 2 OS e nenhuma carga real — a tela precisa dizer isso."""
        data = compute_os_carga_atual(cenario_os, agora=AGORA)
        por_pessoa = {p["nome"]: p for p in data["por_pessoa"]}
        assert por_pessoa["Rodrigo"]["fila_parada"] is True
        assert por_pessoa["Gislaine"]["fila_parada"] is False

    def test_unassigned_orders_get_readable_bucket(
        self, cenario_os: Organization
    ) -> None:
        data = compute_os_carga_atual(cenario_os, agora=AGORA)
        nomes = {p["nome"] for p in data["por_pessoa"]}
        assert "(Sem técnico)" in nomes

    def test_backlog_composition_by_subject(self, cenario_os: Organization) -> None:
        data = compute_os_carga_atual(cenario_os, agora=AGORA)
        backlog = {b["nome"]: b["n"] for b in data["backlog_por_tipo"]}
        assert backlog == {"Retirada de Equipamentos": 3}
        # A fila viva tem composição própria — não se mistura com o backlog.
        fila = {b["nome"]: b["n"] for b in data["fila_por_tipo"]}
        assert fila["Manutenção Técnica"] == 3

    def test_order_without_opened_at_counts_as_backlog(
        self, cenario_os: Organization
    ) -> None:
        """Sem data de abertura não dá pra provar que é recente — balde conservador."""
        set_current_organization(cenario_os)
        Ticket.objects.create(
            organization=cenario_os,
            source_type=SourceType.IXC.value,
            external_id="8",
            customer_external_id="c1",
            status=Ticket.Status.OPEN.value,
            opened_at=None,
        )
        data = compute_os_carga_atual(cenario_os, agora=AGORA)
        assert data["backlog"] == 4

    def test_isolated_by_organization(
        self, cenario_os: Organization, organization_b: Organization
    ) -> None:
        set_current_organization(organization_b)
        data = compute_os_carga_atual(organization_b, agora=AGORA)
        assert data["total"] == 0
        assert data["por_pessoa"] == []


@pytest.mark.django_db
@pytest.mark.filterwarnings("ignore:No directory at:UserWarning")
class TestOsPageRendersCarga:
    def test_page_shows_current_load(self, client, cenario_os: Organization) -> None:
        from apps.tenancy.models import OrganizationMembership, User

        user = User.objects.create_user(email="op@velus.test", password="x")
        OrganizationMembership.objects.create(
            user=user,
            organization=cenario_os,
            role=OrganizationMembership.Role.OWNER,
            is_active=True,
        )
        client.force_login(user)

        response = client.get("/operations/os/")

        assert response.status_code == 200
        corpo = response.content.decode()
        assert "OS abertas por pessoa" in corpo
        assert "SLA de agenda" in corpo
