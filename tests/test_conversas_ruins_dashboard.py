"""Testes do dashboard de "conversas ruins" priorizadas por MRR — issue #49.

Cobre a view `conversas_ruins` (score heurístico × MRR, filtros) e o drill-down
`atendimento_detail` (contexto de receita/risco + timeline de mensagens já
persistidas — sem tocar a rede pois não há OrganizationDataSource OPA no teste).
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any

import pytest
from django.utils import timezone

from apps.analytics.infrastructure.models import ChurnRiskScore
from apps.atendimento.infrastructure.models import (
    Atendimento,
    Departamento,
    Mensagem,
)
from apps.customers.infrastructure.models import Contract, Customer
from apps.shared.context import set_current_organization
from apps.tenancy.models import Organization, User


def _customer(org: Organization, *, document: str, name: str) -> Customer:
    set_current_organization(org)
    return Customer.objects.create(
        organization=org,
        source_type="IXC",
        external_id=f"ixc-{document}",
        document=document,
        name=name,
        status=Customer.Status.ACTIVE.value,
    )


def _departamento(org: Organization, *, external_id: str, nome: str) -> Departamento:
    set_current_organization(org)
    return Departamento.objects.create(
        organization=org, source_type="OPA", external_id=external_id, nome=nome,
    )


def _atendimento(
    org: Organization,
    *,
    external_id: str,
    departamento: Departamento | None = None,
    customer: Customer | None = None,
    document: str = "",
    status: str = "CLOSED",
    rating: int | None = None,
    opened_offset_days: int = 5,
    resolution_hours: float | None = None,
    atendente_nome: str = "",
) -> Atendimento:
    set_current_organization(org)
    now = timezone.now()
    opened_at = now - timedelta(days=opened_offset_days)
    closed_at = None
    if status == "CLOSED" and resolution_hours is not None:
        closed_at = opened_at + timedelta(hours=resolution_hours)
    return Atendimento.objects.create(
        organization=org,
        source_type="OPA",
        external_id=external_id,
        departamento=departamento,
        departamento_external_id=departamento.external_id if departamento else "",
        customer=customer,
        customer_document=document,
        customer_name=customer.name if customer else "",
        status=status,
        canal="whatsapp",
        protocol=f"OPA-{external_id}",
        rating=rating,
        atendente_nome=atendente_nome,
        opened_at=opened_at,
        closed_at=closed_at,
    )


def _risk(
    org: Organization, customer: Customer, *, level: str, mrr: float, score: int
) -> ChurnRiskScore:
    set_current_organization(org)
    return ChurnRiskScore.objects.create(
        organization=org,
        customer=customer,
        score=score,
        level=level,
        monthly_amount=mrr,
        signals=[{"code": "LATE_PAYMENTS", "label": "Atraso recorrente"}],
        computed_at=timezone.now(),
    )


@pytest.fixture
def seeded(organization_a: Organization) -> dict[str, Any]:
    suporte = _departamento(organization_a, external_id="dep-sup", nome="Suporte")
    ouvidoria = _departamento(organization_a, external_id="dep-ouv", nome="Ouvidoria")

    # Cliente caro e arriscado, nota baixa → deve liderar o ranking.
    caro = _customer(organization_a, document="11111111111", name="Cliente Caro")
    _risk(organization_a, caro, level="HIGH", mrr=500.0, score=80)
    at_caro = _atendimento(
        organization_a, external_id="a1", departamento=suporte, customer=caro,
        document="11111111111", rating=1, resolution_hours=2,
    )

    # Cliente barato, ouvidoria → conversa ruim mas pesa menos.
    barato = _customer(organization_a, document="22222222222", name="Cliente Barato")
    Contract.objects.create(
        organization=organization_a, source_type="IXC", external_id="ct-2",
        customer=barato, customer_external_id="ixc-22222222222",
        plan_name="Basico", monthly_amount=80, status="ACTIVE",
    )
    _atendimento(
        organization_a, external_id="a2", departamento=ouvidoria, customer=barato,
        document="22222222222", rating=4, resolution_hours=1,
    )

    # Conversa boa (nota 5, sem sinal) → não entra na lista.
    bom = _customer(organization_a, document="33333333333", name="Cliente Bom")
    _atendimento(
        organization_a, external_id="a3", departamento=suporte, customer=bom,
        document="33333333333", rating=5, resolution_hours=1,
    )
    return {"caro": caro, "barato": barato, "at_caro": at_caro, "suporte": suporte}


@pytest.mark.django_db
@pytest.mark.filterwarnings("ignore:No directory at:UserWarning")
class TestConversasRuinsView:
    def test_requires_login(self, client: Any) -> None:
        resp = client.get("/operations/conversas-ruins/")
        assert resp.status_code == 302

    def test_ranks_by_mrr_times_score(
        self, client: Any, user_a: User, seeded: dict[str, Any]
    ) -> None:
        client.force_login(user_a)
        resp = client.get("/operations/conversas-ruins/")
        assert resp.status_code == 200
        body = resp.content
        # Conversa boa (nota 5) não aparece; as duas ruins sim.
        assert b"Cliente Caro" in body
        assert b"Cliente Barato" in body
        assert b"Cliente Bom" not in body
        # Cliente Caro (MRR 500 × score alto) deve vir antes do Barato.
        assert body.index(b"Cliente Caro") < body.index(b"Cliente Barato")

    def test_filter_by_departamento(
        self, client: Any, user_a: User, seeded: dict[str, Any]
    ) -> None:
        client.force_login(user_a)
        resp = client.get(
            f"/operations/conversas-ruins/?departamento={seeded['suporte'].id}"
        )
        assert resp.status_code == 200
        # Só Suporte → o atendimento de Ouvidoria (Cliente Barato) some.
        assert b"Cliente Caro" in resp.content
        assert b"Cliente Barato" not in resp.content

    def test_empty_org_renders(
        self, client: Any, user_a: User, organization_a: Organization
    ) -> None:
        client.force_login(user_a)
        resp = client.get("/operations/conversas-ruins/")
        assert resp.status_code == 200
        assert b"Nenhuma conversa ruim no per" in resp.content


@pytest.mark.django_db
class TestQualificacaoConversaRuim:
    """Requalificação CX (itens 1+2): desfecho > proxy; bot não é conversa ruim."""

    def _codes(self, rows: list[dict[str, Any]], atendimento_id: int) -> set[str]:
        for r in rows:
            if r["atendimento_id"] == atendimento_id:
                return {s["code"] for s in r["signals"]}
        return set()

    def test_bot_resolvido_nao_e_ruim(self, organization_a: Organization) -> None:
        from apps.analytics.application.aggregations import compute_bad_conversations

        tri = _departamento(organization_a, external_id="dep-tri", nome="Triagem")
        cli = _customer(organization_a, document="44444444444", name="Cliente Bot")
        # Conversa longa atendida pelo bot "Felipe" na Triagem: TMA alto não pune.
        at = _atendimento(
            organization_a, external_id="bot1", departamento=tri, customer=cli,
            document="44444444444", atendente_nome="Felipe", resolution_hours=200,
        )
        data = compute_bad_conversations(organization_a)
        # Sem nota baixa, sem QA, bot+Triagem → nenhum sinal → fora da lista.
        assert self._codes(data["rows"], at.id) == set()

    def test_reabertura_so_entre_humanos_mesmo_setor(
        self, organization_a: Organization
    ) -> None:
        from apps.analytics.application.aggregations import compute_bad_conversations

        sup = _departamento(organization_a, external_id="dep-sup2", nome="Suporte")
        cli = _customer(organization_a, document="55555555555", name="Cliente Volta")
        _atendimento(
            organization_a, external_id="r1", departamento=sup, customer=cli,
            document="55555555555", atendente_nome="Alice", opened_offset_days=8,
            resolution_hours=1,
        )
        segundo = _atendimento(
            organization_a, external_id="r2", departamento=sup, customer=cli,
            document="55555555555", atendente_nome="Alice", opened_offset_days=4,
            resolution_hours=1,
        )
        data = compute_bad_conversations(organization_a)
        # 2º contato humano no mesmo setor < 7d depois do 1º → reabertura.
        assert "reabertura" in self._codes(data["rows"], segundo.id)

    def test_reentrada_no_bot_nao_e_reabertura(
        self, organization_a: Organization
    ) -> None:
        from apps.analytics.application.aggregations import compute_bad_conversations

        tri = _departamento(organization_a, external_id="dep-tri2", nome="Triagem")
        cli = _customer(organization_a, document="66666666666", name="Cliente Menu")
        _atendimento(
            organization_a, external_id="m1", departamento=tri, customer=cli,
            document="66666666666", atendente_nome="Gi", opened_offset_days=6,
            resolution_hours=1,
        )
        segundo = _atendimento(
            organization_a, external_id="m2", departamento=tri, customer=cli,
            document="66666666666", atendente_nome="Gi", opened_offset_days=3,
            resolution_hours=1,
        )
        data = compute_bad_conversations(organization_a)
        # Reentrar no menu do bot 2x não é falha → não vira conversa ruim.
        assert self._codes(data["rows"], segundo.id) == set()

    def test_nao_resolveu_do_juiz_qa_pesa(self, organization_a: Organization) -> None:
        from apps.analytics.application.aggregations import compute_bad_conversations
        from apps.analytics.infrastructure.models import QAReview

        sup = _departamento(organization_a, external_id="dep-sup3", nome="Suporte")
        cli = _customer(organization_a, document="77777777777", name="Cliente NaoRes")
        at = _atendimento(
            organization_a, external_id="q1", departamento=sup, customer=cli,
            document="77777777777", atendente_nome="Marco", resolution_hours=1,
        )
        QAReview.objects.create(
            organization=organization_a, atendimento=at, resolveu=False,
            sla_ok=True, overall_score=30, reviewed_at=timezone.now(),
        )
        data = compute_bad_conversations(organization_a)
        assert "nao_resolveu" in self._codes(data["rows"], at.id)


@pytest.mark.django_db
@pytest.mark.filterwarnings("ignore:No directory at:UserWarning")
class TestAtendimentoDetailView:
    def test_404_for_missing(
        self, client: Any, user_a: User, organization_a: Organization
    ) -> None:
        client.force_login(user_a)
        resp = client.get("/operations/conversas-ruins/999999/")
        assert resp.status_code == 404

    def test_renders_timeline_from_stored_messages(
        self, client: Any, user_a: User, seeded: dict[str, Any], organization_a: Organization
    ) -> None:
        at = seeded["at_caro"]
        set_current_organization(organization_a)
        Mensagem.objects.create(
            organization=organization_a, source_type="OPA", external_id="m1",
            atendimento=at, atendimento_external_id=at.external_id,
            direction="CLIENT", texto="minha internet caiu", sent_at=timezone.now(),
        )
        Mensagem.objects.create(
            organization=organization_a, source_type="OPA", external_id="m2",
            atendimento=at, atendimento_external_id=at.external_id,
            direction="AGENT", texto="vamos verificar", sent_at=timezone.now(),
        )
        client.force_login(user_a)
        resp = client.get(f"/operations/conversas-ruins/{at.id}/")
        assert resp.status_code == 200
        # Timeline mostra as mensagens persistidas e o contexto de receita/risco.
        assert b"minha internet caiu" in resp.content
        assert b"vamos verificar" in resp.content
        assert b"Cliente Caro" in resp.content
        assert b"Alto" in resp.content  # risco HIGH

    def test_renders_without_messages(
        self, client: Any, user_a: User, seeded: dict[str, Any]
    ) -> None:
        # Sem Mensagem persistida e sem datasource OPA → fallback legível, sem rede.
        at = seeded["at_caro"]
        client.force_login(user_a)
        resp = client.get(f"/operations/conversas-ruins/{at.id}/")
        assert resp.status_code == 200
        assert b"Sem mensagens" in resp.content
