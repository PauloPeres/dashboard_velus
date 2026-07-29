"""Testes de `compute_atendimento_horario` (F3 — gráfico horário sazonal).

Cobre a série horária vs baseline sazonal, detecção de anomalia (pico acima da
banda), marcadores de vencimento e o smoke da view.
"""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal
from typing import Any
from zoneinfo import ZoneInfo

import pytest
from django.utils import timezone

from apps.analytics.application.aggregations import compute_atendimento_horario
from apps.atendimento.infrastructure.models import Atendimento
from apps.financial.infrastructure.models import Invoice
from apps.integrations.shared.enums import SourceType
from apps.shared.context import set_current_organization
from apps.tenancy.models import Organization, User

_SP = ZoneInfo("America/Sao_Paulo")


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

    def test_billing_baseline_suppresses_false_anomaly(
        self, organization_a: Organization
    ) -> None:
        """v2: pico num dia de cobrança não vira falsa anomalia; sem a v2, viraria.

        Cria dias de cobrança (35 faturas cada) em dias-da-semana VARIADOS
        (passo de 5 dias), cada um com 10 atendimentos às 12h. O dia de exibição
        (3 dias atrás) é dia de cobrança com 10 às 12h. Com o baseline de
        cobrança (média 10 por hora), esse pico é esperado. Sem a v2 (limiar
        altíssimo), o baseline normal (aquele dia-da-semana × 12h) está vazio →
        o mesmo pico seria anomalia.
        """
        set_current_organization(organization_a)
        base_local = timezone.localtime(timezone.now(), _SP)
        inv = 0
        # n=0..2 caem na janela de exibição (3,8,13 dias); n=3..6 no baseline.
        for n in range(7):
            day = (base_local - timedelta(days=3 + 5 * n)).replace(
                hour=12, minute=0, second=0, microsecond=0
            )
            for _ in range(35):
                inv += 1
                Invoice.objects.create(
                    organization=organization_a,
                    source_type=SourceType.IXC.value,
                    external_id=f"inv{inv}",
                    contract_external_id="c1",
                    amount=Decimal("50.00"),
                    due_date=day.date(),
                    status=Invoice.Status.PENDING.value,
                )
            for i in range(10):
                _at(organization_a, external_id=f"b{n}-{i}", opened_at=day)

        target_label = (base_local - timedelta(days=3)).strftime("%d/%m 12h")

        # v2 ligada (window=0 pra baseline de cobrança limpo, sem diluir vizinhos)
        on = compute_atendimento_horario(
            organization_a, days=14, billing_window_days=0
        )
        idx = on["labels"].index(target_label)
        assert on["is_billing"][idx] is True
        assert on["expected"][idx] == pytest.approx(10, abs=1.0)
        assert target_label not in on["anomaly_x"]

        # v2 desligada (nenhum dia vira "de cobrança") -> baseline normal vazio
        # pra aquele dia-da-semana×12h -> o mesmo pico vira anomalia.
        off = compute_atendimento_horario(
            organization_a, days=14, billing_min_invoices=100000
        )
        idx_off = off["labels"].index(target_label)
        assert off["expected"][idx_off] == pytest.approx(0, abs=0.5)
        assert target_label in off["anomaly_x"]

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
