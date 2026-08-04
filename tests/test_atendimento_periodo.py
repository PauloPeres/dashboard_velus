"""Testes do período único da página de Tendências de Atendimento (#75).

Cobre o helper `_get_period` (presets, personalizado e os casos inválidos),
`compute_atendimento_horario` com `start`/`end` arbitrários e a coerência do
intervalo entre todos os blocos de contexto da view.
"""

from __future__ import annotations

from datetime import date, datetime, time, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import pytest
from django.test import RequestFactory
from django.urls import reverse
from django.utils import timezone

from apps.analytics.application.aggregations import compute_atendimento_horario
from apps.atendimento.infrastructure.models import Atendimento
from apps.dashboards.views import _get_period
from apps.integrations.shared.enums import SourceType
from apps.shared.context import set_current_organization
from apps.tenancy.models import Organization, User

_SP = ZoneInfo("America/Sao_Paulo")
URL = "/operations/atendimento-tendencias/"


def _period(query: str = "") -> Any:
    return _get_period(RequestFactory().get(f"{URL}?{query}"))


def _today() -> date:
    return timezone.localtime(timezone.now(), _SP).date()


def _at(org: Organization, *, external_id: str, opened_at: datetime) -> Atendimento:
    return Atendimento.objects.create(
        organization=org,
        source_type=SourceType.OPA.value,
        external_id=external_id,
        status=Atendimento.Status.CLOSED.value,
        opened_at=opened_at,
    )


class TestGetPeriodPresets:
    """Presets: a janela concreta e o rótulo legível num print."""

    @pytest.mark.parametrize(
        ("key", "n_dias"),
        [("7d", 7), ("14d", 14), ("30d", 30)],
    )
    def test_preset_em_dias(self, key: str, n_dias: int) -> None:
        p = _period(f"periodo={key}")
        today = _today()
        assert p.key == key
        assert p.is_custom is False
        assert p.start_date == today - timedelta(days=n_dias - 1)
        assert p.end_date == today
        assert p.label.startswith(f"Últimos {n_dias} dias (")
        assert p.start_date.strftime("%d/%m/%Y") in p.label
        assert today.strftime("%d/%m/%Y") in p.label

    @pytest.mark.parametrize(("key", "n_meses"), [("3m", 3), ("6m", 6), ("12m", 12)])
    def test_preset_em_meses(self, key: str, n_meses: int) -> None:
        p = _period(f"periodo={key}")
        today = _today()
        assert p.key == key
        assert p.end_date == today
        # ~N meses de janela (variação de dias por mês).
        assert 28 * n_meses <= (today - p.start_date).days + 1 <= 31 * n_meses + 1
        assert p.label.startswith(f"Últimos {n_meses} meses (")

    def test_default_sem_parametro(self) -> None:
        p = _period()
        assert p.key == "30d"
        assert p.warning is None

    def test_preset_invalido_cai_no_default(self) -> None:
        assert _period("periodo=banana").key == "30d"
        assert _period("periodo=0d").key == "30d"
        assert _period("periodo=99m").key == "30d"  # acima do teto de 24 meses

    def test_query_reproduz_o_preset(self) -> None:
        assert _period("periodo=7d").query == "periodo=7d"


class TestGetPeriodCustom:
    """Período personalizado `?de=&ate=` — válido e os quatro casos inválidos."""

    def test_range_valido(self) -> None:
        today = _today()
        de = today - timedelta(days=2)
        p = _period(f"de={de.isoformat()}&ate={today.isoformat()}")
        assert p.is_custom is True
        assert p.start_date == de
        assert p.end_date == today
        assert p.warning is None
        assert p.label == (
            f"Período: {de.strftime('%d/%m/%Y')} – {today.strftime('%d/%m/%Y')}"
        )
        assert p.query == f"de={de.isoformat()}&ate={today.isoformat()}"

    def test_datas_sao_inclusivas_no_fuso_de_sao_paulo(self) -> None:
        de = _today() - timedelta(days=3)
        ate = _today() - timedelta(days=1)
        p = _period(f"de={de.isoformat()}&ate={ate.isoformat()}")
        assert timezone.localtime(p.start, _SP) == datetime.combine(
            de, time.min, tzinfo=_SP
        )
        assert timezone.localtime(p.end, _SP) == datetime.combine(
            ate, time.max, tzinfo=_SP
        )

    def test_fim_nunca_passa_de_agora(self) -> None:
        today = _today()
        p = _period(f"de={today.isoformat()}&ate={today.isoformat()}")
        assert p.end <= timezone.now()

    def test_fim_antes_do_inicio(self) -> None:
        today = _today()
        de = today - timedelta(days=1)
        p = _period(f"de={de.isoformat()}&ate={(de - timedelta(days=5)).isoformat()}")
        assert p.key == "30d"
        assert p.warning is not None

    def test_fim_no_futuro(self) -> None:
        today = _today()
        p = _period(
            f"de={today.isoformat()}&ate={(today + timedelta(days=1)).isoformat()}"
        )
        assert p.key == "30d"
        assert p.warning is not None

    def test_range_maior_que_24_meses(self) -> None:
        today = _today()
        de = today - timedelta(days=800)
        p = _period(f"de={de.isoformat()}&ate={today.isoformat()}")
        assert p.key == "30d"
        assert p.warning is not None

    def test_data_malformada(self) -> None:
        p = _period("de=ontem&ate=hoje")
        assert p.key == "30d"
        assert p.warning is not None


class TestGetPeriodLegacy:
    """URLs antigas `?months=` / `?hd=` continuam abrindo a página (#75)."""

    def test_months_legado_vira_preset_em_meses(self) -> None:
        p = _period("months=6")
        assert p.key == "6m"
        assert p.warning is None

    def test_hd_legado_vira_preset_em_dias(self) -> None:
        p = _period("hd=7")
        assert p.key == "7d"

    def test_months_vence_hd_quando_ambos_presentes(self) -> None:
        assert _period("months=3&hd=7").key == "3m"

    def test_periodo_novo_vence_o_legado(self) -> None:
        assert _period("periodo=14d&months=12&hd=30").key == "14d"

    def test_custom_vence_tudo(self) -> None:
        today = _today()
        de = today - timedelta(days=1)
        p = _period(f"de={de.isoformat()}&ate={today.isoformat()}&periodo=12m&hd=7")
        assert p.is_custom is True


@pytest.mark.django_db
class TestHorarioComIntervaloArbitrario:
    def test_numero_de_slots_bate_com_o_intervalo(
        self, organization_a: Organization
    ) -> None:
        set_current_organization(organization_a)
        p = _period(
            f"de={(_today() - timedelta(days=2)).isoformat()}"
            f"&ate={(_today() - timedelta(days=1)).isoformat()}"
        )
        data = compute_atendimento_horario(
            organization_a, start=p.start, end=p.end, foco="todos"
        )
        # 2 dias inclusivos, hora a hora: 48 slots (00h..23h de cada dia).
        assert data["n_slots"] == 48
        assert len(data["labels"]) == 48
        assert data["days"] == 2
        assert data["labels"][0].endswith("00h")
        assert data["labels"][-1].endswith("23h")

    def test_conta_so_o_que_esta_dentro_da_janela(
        self, organization_a: Organization
    ) -> None:
        set_current_organization(organization_a)
        dentro = datetime.combine(
            _today() - timedelta(days=2), time(hour=10), tzinfo=_SP
        )
        fora = datetime.combine(
            _today() - timedelta(days=5), time(hour=10), tzinfo=_SP
        )
        _at(organization_a, external_id="in1", opened_at=dentro)
        _at(organization_a, external_id="out1", opened_at=fora)

        p = _period(
            f"de={(_today() - timedelta(days=3)).isoformat()}"
            f"&ate={(_today() - timedelta(days=1)).isoformat()}"
        )
        data = compute_atendimento_horario(
            organization_a, start=p.start, end=p.end, foco="todos"
        )
        assert data["total_janela"] == 1

    def test_baseline_vem_das_semanas_anteriores_ao_inicio(
        self, organization_a: Organization
    ) -> None:
        """O esperado do slot sai das 8 semanas ANTES do início da janela."""
        set_current_organization(organization_a)
        alvo = datetime.combine(_today() - timedelta(days=2), time(hour=12), tzinfo=_SP)
        # As 8 semanas do mesmo dia-da-semana × 12h ANTERIORES ao início da
        # janela, 10 atendimentos cada -> esperado ~10 (descarta 1 outlier).
        n = 0
        for semana in range(1, 9):
            dia = alvo - timedelta(weeks=semana)
            for _ in range(10):
                n += 1
                _at(organization_a, external_id=f"b{n}", opened_at=dia)
        _at(organization_a, external_id="alvo", opened_at=alvo)

        p = _period(
            f"de={(_today() - timedelta(days=2)).isoformat()}"
            f"&ate={(_today() - timedelta(days=1)).isoformat()}"
        )
        data = compute_atendimento_horario(
            organization_a, start=p.start, end=p.end, foco="todos"
        )
        idx = data["labels"].index(alvo.strftime("%d/%m %Hh"))
        assert data["expected"][idx] == pytest.approx(10, abs=1.0)
        # Só o atendimento da janela entra na série real — o baseline fica fora.
        assert data["total_janela"] == 1


@pytest.mark.django_db
@pytest.mark.filterwarnings("ignore:No directory at:UserWarning")
class TestPeriodoNaView:
    def test_mesma_janela_em_todos_os_blocos(
        self, client: Any, user_a: User, organization_a: Organization
    ) -> None:
        client.force_login(user_a)
        de = _today() - timedelta(days=5)
        ate = _today() - timedelta(days=1)
        resp = client.get(f"{URL}?de={de.isoformat()}&ate={ate.isoformat()}")
        assert resp.status_code == 200
        ctx = resp.context
        period = ctx["period"]
        assert period.start_date == de
        assert period.end_date == ate
        # Horário e motivos/tags derivam do MESMO intervalo — nada divergente.
        assert ctx["horario"]["window_start"] == period.start
        assert ctx["horario"]["window_end"].date() == ate
        assert ctx["window_start"] == period.start
        assert ctx["window_end"] == period.end

    def test_periodo_aparece_escrito_na_tela(
        self, client: Any, user_a: User, organization_a: Organization
    ) -> None:
        client.force_login(user_a)
        resp = client.get(f"{URL}?periodo=7d")
        assert resp.status_code == 200
        assert resp.context["period_label"].encode() in resp.content

    def test_datas_invalidas_nao_quebram_a_pagina(
        self, client: Any, user_a: User, organization_a: Organization
    ) -> None:
        client.force_login(user_a)
        futuro = (_today() + timedelta(days=3)).isoformat()
        resp = client.get(f"{URL}?de={_today().isoformat()}&ate={futuro}")
        assert resp.status_code == 200
        assert resp.context["period"].key == "30d"
        assert resp.context["period_warning"].encode() in resp.content

    def test_sem_seletor_global_de_periodo_na_sidebar(
        self, client: Any, user_a: User, organization_a: Organization
    ) -> None:
        """A página tem período próprio — o seletor global da sidebar sai (#75).

        Dois controles de período na mesma tela seriam contraditórios (e o da
        sidebar, que escreve ?months=, viraria controle morto).
        """
        client.force_login(user_a)
        resp = client.get(URL)
        assert resp.status_code == 200
        assert b"setPeriodMonths(" not in resp.content

    def test_outras_paginas_mantem_o_seletor_global(
        self, client: Any, user_a: User, organization_a: Organization
    ) -> None:
        client.force_login(user_a)
        resp = client.get(reverse("dashboards:executive"))
        assert resp.status_code == 200
        assert b"setPeriodMonths(" in resp.content

    def test_url_legada_continua_abrindo(
        self, client: Any, user_a: User, organization_a: Organization
    ) -> None:
        client.force_login(user_a)
        resp = client.get(f"{URL}?months=6&hd=7")
        assert resp.status_code == 200
        assert resp.context["period"].key == "6m"
