"""Filtro de período como componente reutilizável (#86).

Cobre o que #75 não tinha: presets Hoje/Ontem, persistência por cookie (com a
URL sempre vencendo), granularidade mensal das páginas de série, propagação dos
params extras e o render dos partials.
"""

from __future__ import annotations

from datetime import date, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import pytest
from django.template.loader import render_to_string
from django.test import RequestFactory
from django.urls import reverse
from django.utils import timezone

from apps.dashboards.period import (
    COOKIE_NAME,
    DAY_PRESETS,
    MONTH_PRESETS,
    Period,
    get_period,
    period_context,
    set_period_extra_params,
)
from apps.tenancy.models import Organization, User

_SP = ZoneInfo("America/Sao_Paulo")
TENDENCIAS = "/operations/atendimento-tendencias/"


def _today() -> date:
    return timezone.localtime(timezone.now(), _SP).date()


def _req(query: str = "", cookie: str | None = None) -> Any:
    request = RequestFactory().get(f"{TENDENCIAS}?{query}")
    if cookie is not None:
        request.COOKIES[COOKIE_NAME] = cookie
    return request


def _period(query: str = "", cookie: str | None = None, **kwargs: Any) -> Period:
    return get_period(_req(query, cookie), **kwargs)


class TestPresetsNovos:
    def test_hoje(self) -> None:
        p = _period("periodo=1d")
        assert p.key == "1d"
        assert p.start_date == _today()
        assert p.end_date == _today()
        assert p.label.startswith("Hoje (")

    def test_ontem(self) -> None:
        ontem = _today() - timedelta(days=1)
        p = _period("periodo=ontem")
        assert p.key == "ontem"
        assert p.start_date == ontem
        assert p.end_date == ontem
        assert p.label == f"Ontem ({ontem.strftime('%d/%m/%Y')})"
        # Chave própria (e não `de=&ate=`): o link continua relativo.
        assert p.query == "periodo=ontem"

    def test_ontem_cobre_o_dia_inteiro_no_fuso_de_sao_paulo(self) -> None:
        p = _period("periodo=ontem")
        assert timezone.localtime(p.start, _SP).hour == 0
        assert timezone.localtime(p.end, _SP).hour == 23

    def test_lista_de_presets_da_pagina_em_dias(self) -> None:
        keys = [k for k, _ in DAY_PRESETS]
        assert keys == ["1d", "ontem", "7d", "14d", "30d", "3m", "6m", "12m"]


class TestPrecedenciaCookie:
    def test_url_vence_o_cookie(self) -> None:
        assert _period("periodo=7d", cookie="12m").key == "7d"

    def test_custom_na_url_vence_o_cookie(self) -> None:
        de = _today() - timedelta(days=3)
        p = _period(
            f"de={de.isoformat()}&ate={_today().isoformat()}", cookie="ontem"
        )
        assert p.is_custom is True
        assert p.start_date == de

    def test_legado_na_url_vence_o_cookie(self) -> None:
        assert _period("months=6", cookie="7d").key == "6m"

    def test_cookie_vence_o_default(self) -> None:
        assert _period(cookie="7d").key == "7d"
        assert _period(cookie="ontem").key == "ontem"

    def test_cookie_com_periodo_personalizado(self) -> None:
        de = _today() - timedelta(days=4)
        ate = _today() - timedelta(days=1)
        p = _period(cookie=f"custom:{de.isoformat()}:{ate.isoformat()}")
        assert p.is_custom is True
        assert p.start_date == de
        assert p.end_date == ate

    @pytest.mark.parametrize(
        "raw",
        ["banana", "", "0d", "99m", "custom:2026-13-01:2026-01-01", "custom:x:y"],
    )
    def test_cookie_invalido_e_ignorado_em_silencio(self, raw: str) -> None:
        p = _period(cookie=raw)
        assert p.key == "30d"
        assert p.warning is None  # o usuário não digitou aquilo

    def test_cookie_com_data_futura_e_ignorado_em_silencio(self) -> None:
        futuro = (_today() + timedelta(days=5)).isoformat()
        p = _period(cookie=f"custom:{_today().isoformat()}:{futuro}")
        assert p.key == "30d"
        assert p.warning is None

    def test_sem_url_e_sem_cookie_cai_no_default(self) -> None:
        assert _period().key == "30d"

    def test_so_a_url_marca_o_cookie_pra_gravar(self) -> None:
        req = _req("periodo=7d")
        get_period(req)
        assert req.velus_period_cookie == "7d"

        req = _req(cookie="7d")
        get_period(req)
        assert getattr(req, "velus_period_cookie", None) is None


class TestGranularidadeMensal:
    def test_default_de_12_meses(self) -> None:
        p = _period(granularity="month")
        assert p.key == "12m"
        assert p.months == 12

    def test_months_legado_continua_valendo(self) -> None:
        assert _period("months=6", granularity="month").months == 6
        assert _period("months=24", granularity="month").months == 24

    def test_preset_em_meses_pela_url(self) -> None:
        p = _period("periodo=3m", granularity="month")
        assert p.months == 3
        assert p.label.startswith("3 meses (")

    def test_periodo_em_dias_na_url_avisa_e_cai_no_default(self) -> None:
        p = _period("periodo=ontem", granularity="month")
        assert p.months == 12
        assert p.warning is not None

    def test_periodo_em_dias_no_cookie_e_silencioso(self) -> None:
        p = _period(cookie="ontem", granularity="month")
        assert p.months == 12
        assert p.warning is None

    def test_presets_exibidos_sao_so_os_mensais(self) -> None:
        req = _req()
        get_period(req, granularity="month")
        ctx = period_context(req)
        assert [p["key"] for p in ctx["period_presets"]] == [
            k for k, _ in MONTH_PRESETS
        ]
        assert ctx["period_allow_custom"] is False


class TestContextoDeTemplate:
    def test_params_extras_propagados(self) -> None:
        req = _req("periodo=7d")
        get_period(req)
        set_period_extra_params(
            req, {"g": "week", "foco": "rede", "departamento": None, "tag": ""}
        )
        ctx = period_context(req)
        # Vazios/None não viram hidden input.
        assert ctx["period_extra_params"] == {"g": "week", "foco": "rede"}

    def test_pagina_sem_periodo_nao_tem_contexto(self) -> None:
        assert period_context(_req("periodo=7d")) == {}

    def test_preset_ativo_marcado(self) -> None:
        req = _req("periodo=ontem")
        get_period(req)
        ativos = [p for p in period_context(req)["period_presets"] if p["active"]]
        assert [p["key"] for p in ativos] == ["ontem"]


class TestRenderDosPartials:
    def test_barra_de_filtro(self) -> None:
        req = _req("periodo=7d")
        get_period(req)
        set_period_extra_params(req, {"g": "week", "foco": "rede"})
        html = render_to_string("dashboards/_period_filter.html", period_context(req))
        assert "setPeriodo('ontem')" in html
        assert "setPeriodo('1d')" in html
        assert 'name="g" value="week"' in html
        assert 'name="foco" value="rede"' in html
        assert 'name="de"' in html
        assert 'name="ate"' in html

    def test_barra_sem_personalizado_na_granularidade_mensal(self) -> None:
        req = _req()
        get_period(req, granularity="month")
        html = render_to_string("dashboards/_period_filter.html", period_context(req))
        assert 'name="de"' not in html
        assert "setPeriodo('12m')" in html

    def test_badge_mostra_o_rotulo(self) -> None:
        req = _req("periodo=ontem")
        period = get_period(req)
        html = render_to_string("dashboards/_period_badge.html", period_context(req))
        assert period.label in html

    def test_aviso_aparece_na_barra(self) -> None:
        req = _req("de=ontem&ate=hoje")
        period = get_period(req)
        html = render_to_string("dashboards/_period_filter.html", period_context(req))
        assert period.warning is not None
        assert period.warning in html


@pytest.mark.django_db
@pytest.mark.filterwarnings("ignore:No directory at:UserWarning")
class TestNaView:
    def test_cookie_gravado_ao_escolher_pela_url(
        self, client: Any, user_a: User, organization_a: Organization
    ) -> None:
        client.force_login(user_a)
        resp = client.get(f"{TENDENCIAS}?periodo=ontem")
        assert resp.status_code == 200
        cookie = resp.cookies[COOKIE_NAME]
        assert cookie.value == "ontem"
        assert cookie["httponly"] is True
        assert cookie["samesite"] == "Lax"
        assert cookie["max-age"] == 60 * 60 * 24 * 90

    def test_cookie_atravessa_a_navegacao(
        self, client: Any, user_a: User, organization_a: Organization
    ) -> None:
        client.force_login(user_a)
        client.get(f"{TENDENCIAS}?periodo=7d")
        resp = client.get(TENDENCIAS)
        assert resp.context["period"].key == "7d"

    def test_url_sobrescreve_o_cookie(
        self, client: Any, user_a: User, organization_a: Organization
    ) -> None:
        client.force_login(user_a)
        client.get(f"{TENDENCIAS}?periodo=7d")
        resp = client.get(f"{TENDENCIAS}?periodo=14d")
        assert resp.context["period"].key == "14d"
        assert resp.cookies[COOKIE_NAME].value == "14d"

    def test_cookie_em_dias_nao_estraga_pagina_mensal(
        self, client: Any, user_a: User, organization_a: Organization
    ) -> None:
        """Escolher "Ontem" numa aba não deixa o DRE com 1 dia de série."""
        client.force_login(user_a)
        client.get(f"{TENDENCIAS}?periodo=ontem")
        resp = client.get(reverse("dashboards:dre"))
        assert resp.status_code == 200
        assert resp.context["period"].key == "12m"
        assert resp.context["period_warning"] is None

    def test_sidebar_sem_select_de_meses(
        self, client: Any, user_a: User, organization_a: Organization
    ) -> None:
        client.force_login(user_a)
        for url in (reverse("dashboards:executive"), reverse("dashboards:churn")):
            resp = client.get(url)
            assert resp.status_code == 200
            assert b"setPeriodMonths(" not in resp.content

    def test_pagina_sem_periodo_nao_mostra_barra(
        self, client: Any, user_a: User, organization_a: Organization
    ) -> None:
        client.force_login(user_a)
        resp = client.get(reverse("dashboards:compromissos"))
        assert resp.status_code == 200
        # A função existe no base.html; o que não pode é botão de preset.
        assert b'onclick="setPeriodo(' not in resp.content

    def test_months_legado_continua_abrindo_pagina_mensal(
        self, client: Any, user_a: User, organization_a: Organization
    ) -> None:
        client.force_login(user_a)
        resp = client.get(f"{reverse('dashboards:executive')}?months=6")
        assert resp.status_code == 200
        assert resp.context["period"].months == 6
