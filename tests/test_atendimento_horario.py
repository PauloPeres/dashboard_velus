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
from apps.atendimento.infrastructure.models import Atendimento, Departamento
from apps.financial.infrastructure.models import Invoice
from apps.integrations.shared.enums import SourceType
from apps.shared.context import set_current_organization
from apps.tenancy.models import Organization, User

_SP = ZoneInfo("America/Sao_Paulo")


def _at(
    org: Organization,
    *,
    external_id: str,
    opened_at: Any,
    departamento: Departamento | None = None,
    tags: list[str] | None = None,
    motivos: list[str] | None = None,
) -> Atendimento:
    return Atendimento.objects.create(
        organization=org,
        source_type=SourceType.OPA.value,
        external_id=external_id,
        status=Atendimento.Status.CLOSED.value,
        opened_at=opened_at,
        departamento=departamento,
        tags=tags or [],
        motivos=motivos or [],
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

        data = compute_atendimento_horario(organization_a, days=14, foco="todos")
        assert data["n_anomalias"] >= 1
        assert max(data["anomaly_y"]) == 5
        assert data["total_janela"] == 5

    def test_no_anomaly_when_within_baseline(
        self, organization_a: Organization
    ) -> None:
        set_current_organization(organization_a)
        # 1 atendimento isolado (val=1) não vira anomalia (piso absoluto = 5).
        _at(
            organization_a,
            external_id="a1",
            opened_at=timezone.now() - timedelta(days=1),
        )
        data = compute_atendimento_horario(organization_a, days=14, foco="todos")
        assert data["n_anomalias"] == 0

    def test_foco_suporte_filtra_departamento(
        self, organization_a: Organization
    ) -> None:
        set_current_organization(organization_a)
        now = timezone.now()
        sup = Departamento.objects.create(
            organization=organization_a, source_type=SourceType.OPA.value,
            external_id="dsup", nome="Suporte",
        )
        com = Departamento.objects.create(
            organization=organization_a, source_type=SourceType.OPA.value,
            external_id="dcom", nome="Comercial",
        )
        _at(organization_a, external_id="s1", opened_at=now - timedelta(days=1),
            departamento=sup)
        _at(organization_a, external_id="c1", opened_at=now - timedelta(days=1),
            departamento=com)
        data = compute_atendimento_horario(organization_a, days=14, foco="suporte")
        assert data["foco"] == "suporte"
        assert data["total_janela"] == 1  # só o do Suporte

    def test_foco_rede_filtra_por_motivo_ou_tag(
        self, organization_a: Organization
    ) -> None:
        set_current_organization(organization_a)
        now = timezone.now()
        # rede via tag, rede via motivo, e um não-rede.
        _at(organization_a, external_id="r1", opened_at=now - timedelta(days=1),
            tags=["Sem Conexão"])
        _at(organization_a, external_id="r2", opened_at=now - timedelta(days=1),
            motivos=["Quedas"])
        _at(organization_a, external_id="x1", opened_at=now - timedelta(days=1),
            tags=["Financeiro em Atraso"], motivos=["Financeiro"])
        data = compute_atendimento_horario(organization_a, days=14, foco="rede")
        assert data["foco"] == "rede"
        assert data["total_janela"] == 2  # só os de rede

    def _seed_comercial_baseline(
        self, org: Organization, com: Departamento, disp: Any, disp_count: int
    ) -> None:
        # Baseline mesmo dia-da-semana×12h, 21..63 dias antes do slot de exibição
        # (todos garantidamente na janela de baseline, sem overlap), 10 cada ->
        # esperado ~10. Depois `disp_count` atendimentos no slot de exibição.
        n = 0
        for off in range(21, 64, 7):
            day = disp - timedelta(days=off)
            for _ in range(10):
                n += 1
                _at(org, external_id=f"b{off}-{n}", opened_at=day, departamento=com)
        for i in range(disp_count):
            _at(org, external_id=f"d{i}", opened_at=disp, departamento=com)

    def test_foco_comercial_detecta_queda(
        self, organization_a: Organization
    ) -> None:
        set_current_organization(organization_a)
        com = Departamento.objects.create(
            organization=organization_a, source_type=SourceType.OPA.value,
            external_id="dcom", nome="Comercial",
        )
        disp = (timezone.localtime(timezone.now(), _SP) - timedelta(days=5)).replace(
            hour=12, minute=0, second=0, microsecond=0
        )
        # esperado ~10, real 2 -> queda (2 <= 10*0.34).
        self._seed_comercial_baseline(organization_a, com, disp, disp_count=2)
        data = compute_atendimento_horario(
            organization_a, days=14, foco="comercial"
        )
        assert data["detect"] == "drop"
        assert disp.strftime("%d/%m 12h") in data["anomaly_x"]

    def test_foco_comercial_sem_queda_quando_normal(
        self, organization_a: Organization
    ) -> None:
        set_current_organization(organization_a)
        com = Departamento.objects.create(
            organization=organization_a, source_type=SourceType.OPA.value,
            external_id="dcom", nome="Comercial",
        )
        disp = (timezone.localtime(timezone.now(), _SP) - timedelta(days=5)).replace(
            hour=12, minute=0, second=0, microsecond=0
        )
        # real 9 ~ no patamar -> não é queda.
        self._seed_comercial_baseline(organization_a, com, disp, disp_count=9)
        data = compute_atendimento_horario(
            organization_a, days=14, foco="comercial"
        )
        assert disp.strftime("%d/%m 12h") not in data["anomaly_x"]

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
            organization_a, days=14, billing_window_days=0, foco="todos"
        )
        idx = on["labels"].index(target_label)
        assert on["is_billing"][idx] is True
        assert on["expected"][idx] == pytest.approx(10, abs=1.0)
        assert target_label not in on["anomaly_x"]

        # v2 desligada (nenhum dia vira "de cobrança") -> baseline normal vazio
        # pra aquele dia-da-semana×12h -> o mesmo pico vira anomalia.
        off = compute_atendimento_horario(
            organization_a, days=14, billing_min_invoices=100000, foco="todos"
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
