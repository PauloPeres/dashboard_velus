"""O placar do modelo de churn na tela — "o risco apontado virou cancelamento?"

Enquanto isto não existia, o churn era uma opinião sem retorno: apontava
clientes e ninguém sabia se acertava. O backtest existia desde a #125, mas só
por linha de comando — rodava, imprimia no terminal e o número morria ali.

O que estes testes travam é a honestidade do placar: lift abaixo de 1,0 aparece
como sinal que aponta para o lado errado, e a validação sem reconstrução (o
score realmente atribuído) não se mistura com os sinais reconstruídos.
"""

from __future__ import annotations

from datetime import date, timedelta
from typing import Any

import pytest
from django.utils import timezone

from apps.analytics.infrastructure.models import ChurnBacktestRun
from apps.dashboards.views import _placar_do_churn
from apps.shared.context import set_current_organization
from apps.tenancy.models import Organization, User


def _run(org: Organization, *, d0: date, sinais: list[dict]) -> ChurnBacktestRun:
    set_current_organization(org)
    return ChurnBacktestRun.objects.create(
        organization=org,
        d0=d0,
        horizon_days=90,
        base_size=2800,
        canceled=84,
        base_rate=0.03,
        signals=sinais,
    )


@pytest.mark.django_db
class TestPlacarDoChurn:
    def test_sem_backtest_a_tela_nao_inventa(self, organization_a: Organization) -> None:
        set_current_organization(organization_a)
        assert _placar_do_churn(organization_a)["tem"] is False

    def test_mostra_o_ultimo_backtest(self, organization_a: Organization) -> None:
        hoje = timezone.now().date()
        _run(organization_a, d0=hoje - timedelta(days=200), sinais=[])
        _run(organization_a, d0=hoje - timedelta(days=90), sinais=[])
        placar = _placar_do_churn(organization_a)
        assert placar["d0"] == hoje - timedelta(days=90)
        assert placar["taxa_base_pct"] == 3.0

    def test_separa_score_atribuido_de_sinal_reconstruido(
        self, organization_a: Organization
    ) -> None:
        """A validação sem reconstrução mede o algoritmo como ele rodou; o resto
        mede como eu suponho que teria rodado. Misturar as duas como se fossem a
        mesma evidência seria o erro."""
        _run(
            organization_a,
            d0=timezone.now().date() - timedelta(days=90),
            sinais=[
                {"nome": "score do dia: HIGH", "lift": 4.2, "n": 120,
                 "cancelados_no_grupo": 18, "separa": True},
                {"nome": "chamados recorrentes", "lift": 1.8, "n": 300,
                 "cancelados_no_grupo": 20, "separa": True},
            ],
        )
        placar = _placar_do_churn(organization_a)
        assert placar["tem_score"] is True
        assert len(placar["score"]) == 1
        assert len(placar["reconstruidos"]) == 1

    def test_sinal_que_aponta_pro_lado_errado_aparece_como_tal(
        self, organization_a: Organization
    ) -> None:
        """Lift abaixo de 1,0 é o modelo errando de direção — e isso tem que
        aparecer escrito, não virar gráfico bonito."""
        _run(
            organization_a,
            d0=timezone.now().date() - timedelta(days=90),
            sinais=[
                {"nome": "score do dia: MEDIUM", "lift": 0.7, "n": 200,
                 "cancelados_no_grupo": 4, "separa": False},
            ],
        )
        placar = _placar_do_churn(organization_a)
        assert placar["score"][0]["separa"] is False

    def test_tela_de_churn_mostra_o_placar(
        self, client: Any, user_a: User, organization_a: Organization
    ) -> None:
        _run(
            organization_a,
            d0=timezone.now().date() - timedelta(days=90),
            sinais=[
                {"nome": "score do dia: HIGH", "lift": 4.2, "n": 120,
                 "cancelados_no_grupo": 18, "separa": True},
            ],
        )
        client.force_login(user_a)
        html = client.get("/churn/").content.decode()
        assert "O risco apontado virou cancelamento?" in html
        # Vírgula, não ponto: a tela é pt-BR e o Django localiza o número.
        assert "4,2×" in html
        assert "18 de 120 cancelaram" in html
