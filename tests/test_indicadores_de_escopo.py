"""Indicadores de escopo do período — Fase 3 do épico #100.

Decisão do Paulo (11/08): onde o período não faz sentido, **não** aplicar o
filtro — mas **marcar na UI**. O problema não é o número ignorar a janela (um
estoque tem que ignorar mesmo); é o badge no topo dizer "Últimos 7 dias" e o
usuário ler o número de baixo como se fosse desse recorte.

Estes testes prendem o selo "posição de agora" nos blocos que são foto do
momento, e o rótulo dinâmico nos títulos que antes diziam "últimos 12 meses"
fixo, independente do que estivesse selecionado.
"""

from __future__ import annotations

from typing import Any

import pytest
from django.template.loader import render_to_string
from django.urls import reverse

from apps.tenancy.models import Organization, User

_SELO = "posição de agora"


class TestPartialDoSelo:
    def test_texto_padrao_quando_nao_recebe_titulo(self) -> None:
        html = render_to_string("dashboards/_badge_agora.html", {})
        assert _SELO in html
        assert "não responde ao filtro de período" in html

    def test_titulo_customizado_vai_pro_tooltip(self) -> None:
        html = render_to_string(
            "dashboards/_badge_agora.html", {"titulo": "Estoque de agora."}
        )
        assert "Estoque de agora." in html


class TestKpiCard:
    def test_card_comum_nao_ganha_selo(self) -> None:
        html = render_to_string(
            "dashboards/_kpi_card.html", {"label": "MRR", "value": "R$ 10"}
        )
        assert _SELO not in html

    def test_card_marcado_ganha_selo_e_tooltip(self) -> None:
        html = render_to_string(
            "dashboards/_kpi_card.html",
            {
                "label": "MRR",
                "value": "R$ 10",
                "agora": True,
                "agora_titulo": "MRR de hoje.",
            },
        )
        assert _SELO in html
        assert "MRR de hoje." in html


@pytest.mark.django_db
@pytest.mark.filterwarnings("ignore:No directory at:UserWarning")
class TestExecutivo:
    """A página onde o badge mais mentia: todos os KPIs ignoram o período."""

    def test_todos_os_kpis_de_estoque_ficam_marcados(
        self, client: Any, user_a: User, organization_a: Organization
    ) -> None:
        client.force_login(user_a)
        body = client.get(reverse("dashboards:executive")).content.decode()
        # 9 cards do partial + "Contratos Ativos" (card manual) + aging.
        assert body.count(_SELO) == 11

    def test_titulos_dos_graficos_dizem_o_periodo_escolhido(
        self, client: Any, user_a: User, organization_a: Organization
    ) -> None:
        client.force_login(user_a)
        resp = client.get(f"{reverse('dashboards:executive')}?periodo=3m")
        body = resp.content.decode()
        label = resp.context["period_label"]
        assert label.startswith("3 meses (")
        # Antes era "MRR — últimos 12 meses" fixo, mentindo com ?periodo=3m.
        assert f"MRR — {label}" in body
        assert f"Contratos por status — {label}" in body
        assert "últimos 12 meses" not in body

    def test_graficos_de_serie_continuam_sem_selo(
        self, client: Any, user_a: User, organization_a: Organization
    ) -> None:
        """Só o que ignora a janela é marcado — série mensal responde a ela."""
        client.force_login(user_a)
        body = client.get(reverse("dashboards:executive")).content.decode()
        i = body.index("MRR — ")
        titulo_mrr = body[i : body.index("</h2>", i)]
        assert _SELO not in titulo_mrr


@pytest.mark.django_db
@pytest.mark.filterwarnings("ignore:No directory at:UserWarning")
class TestFinanceiro:
    """Inadimplência é estoque; recuperação é coorte de janela fixa."""

    def test_blocos_de_estoque_e_de_coorte_ficam_marcados(
        self, client: Any, user_a: User, organization_a: Organization
    ) -> None:
        client.force_login(user_a)
        body = client.get(reverse("dashboards:financial")).content.decode()
        # 5 KPIs de estoque + aging + top 50 devedores.
        assert body.count(_SELO) == 7
        # Recovery rate e a curva de recuperação não são "agora": são uma coorte
        # fixa de 12 meses, então o selo diz outra coisa.
        assert body.count("janela fixa") == 3

    def test_tooltip_explica_a_coorte_do_recovery(
        self, client: Any, user_a: User, organization_a: Organization
    ) -> None:
        client.force_login(user_a)
        body = client.get(reverse("dashboards:financial")).content.decode()
        assert "Coorte fixa dos últimos 12 meses" in body

    def test_titulo_de_bloqueados_deixa_de_dizer_12_meses_fixo(
        self, client: Any, user_a: User, organization_a: Organization
    ) -> None:
        client.force_login(user_a)
        resp = client.get(f"{reverse('dashboards:financial')}?periodo=3m")
        body = resp.content.decode()
        assert f"Contratos bloqueados — {resp.context['period_label']}" in body
        assert "evolução 12 meses" not in body

    def test_graficos_que_seguem_o_filtro_nao_sao_marcados(
        self, client: Any, user_a: User, organization_a: Organization
    ) -> None:
        client.force_login(user_a)
        body = client.get(reverse("dashboards:financial")).content.decode()
        for titulo in ("Recebimentos mensais", "Inadimplência — por mês de vencimento"):
            i = body.index(titulo)
            assert _SELO not in body[i : body.index("</h2>", i)]
