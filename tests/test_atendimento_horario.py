"""Testes de `compute_atendimento_horario` (F3 — gráfico horário sazonal).

Cobre a série horária vs baseline sazonal, detecção de anomalia (pico acima da
banda), marcadores de vencimento e o smoke da view.
"""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal
from typing import Any

import pytest
from django.utils import timezone

from apps.analytics.application.aggregations import compute_atendimento_horario
from apps.atendimento.infrastructure.models import Atendimento
from apps.financial.infrastructure.models import Invoice
from apps.integrations.shared.enums import SourceType
from apps.shared.context import set_current_organization
from apps.tenancy.models import Organization, User


def _at(org: Organization, *, external_id: str, opened_at: Any) -> Atendimento:
    return Atendimento.objects.create(
        organization=org,
        source_type=SourceType.OPA.value,
        external_id=external_id,
        status=Atendimento.Status.CLOSED.value,
        opened_at=opened_at,
    )


@pytest.mark.django_db
class TestAtendimentoHorario:
    def test_arrays_aligned_and_window_valid(
        self, organization_a: Organization
    ) -> None:
        set_current_organization(organization_a)
        data = compute_atendimento_horario(organization_a, days=7)
        n = len(data["labels"])
        assert n > 0
        for key in ("actual", "expected", "upper", "lower"):
            assert len(data[key]) == n
        assert data["days"] == 7

    def test_invalid_days_falls_back_to_14(
        self, organization_a: Organization
    ) -> None:
        set_current_organization(organization_a)
        data = compute_atendimento_horario(organization_a, days=99)
        assert data["days"] == 14

    def test_spike_flagged_as_anomaly(self, organization_a: Organization) -> None:
        set_current_organization(organization_a)
        # Baseline vazio (todos os slots média 0). 5 atendimentos na mesma hora,
        # 2 dias atrás -> pico bem acima da banda -> anomalia.
        spike_at = timezone.now() - timedelta(days=2)
        for i in range(5):
            _at(organization_a, external_id=f"s{i}", opened_at=spike_at)

        data = compute_atendimento_horario(organization_a, days=14)
        assert data["n_anomalias"] >= 1
        assert max(data["anomaly_y"]) == 5
        assert data["total_janela"] == 5

    def test_no_anomaly_when_within_baseline(
        self, organization_a: Organization
    ) -> None:
        set_current_organization(organization_a)
        # 1 atendimento isolado (val=1) não vira anomalia (regra exige val>=2).
        _at(
            organization_a,
            external_id="a1",
            opened_at=timezone.now() - timedelta(days=1),
        )
        data = compute_atendimento_horario(organization_a, days=14)
        assert data["n_anomalias"] == 0

    def test_vencimentos_in_window(self, organization_a: Organization) -> None:
        set_current_organization(organization_a)
        due = (timezone.now() - timedelta(days=3)).date()
        Invoice.objects.create(
            organization=organization_a,
            source_type=SourceType.IXC.value,
            external_id="inv1",
            contract_external_id="c1",
            amount=Decimal("100.00"),
            due_date=due,
            status=Invoice.Status.PENDING.value,
        )
        data = compute_atendimento_horario(organization_a, days=14)
        dates = [v["date"] for v in data["vencimentos"]]
        assert due.strftime("%d/%m") in dates


@pytest.mark.django_db
@pytest.mark.filterwarnings("ignore:No directory at:UserWarning")
class TestHorarioOnTendenciasView:
    URL = "/operations/atendimento-tendencias/"

    def test_page_renders_horario_chart(
        self, client: Any, user_a: User, organization_a: Organization
    ) -> None:
        client.force_login(user_a)
        resp = client.get(f"{self.URL}?hd=7")
        assert resp.status_code == 200
        assert b"horario-chart" in resp.content
        assert b"hora a hora" in resp.content
