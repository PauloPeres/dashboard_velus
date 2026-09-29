"""Página de Auditoria de Churn — a agregação sobre contratos e a view.

As regras de classificação moram em `test_churn_audit_domain.py`; aqui o que se
protege é o recorte: a janela é pela COMPETÊNCIA real (o inadimplente pesa no
mês do bloqueio + 30 dias, não no da baixa), só entra contrato que chegou a ser
ativado, e uma organização nunca vê o cancelamento da outra.
"""

from __future__ import annotations

from datetime import date, datetime, time, timedelta
from decimal import Decimal
from typing import Any
from zoneinfo import ZoneInfo

import pytest
from django.utils import timezone

from apps.analytics.application.churn_audit import (
    Filtros,
    auditar_cancelamentos,
    compute_churn_audit,
)
from apps.analytics.domain.churn_audit import (
    INADIMPLENCIA,
    MUD_SEM_COBERTURA,
    REGRA_BLOQUEIO_AUTO,
    Nivel,
)
from apps.customers.infrastructure.models import Contract
from apps.shared.context import set_current_organization
from apps.tenancy.models import Organization, User

TZ = ZoneInfo("America/Sao_Paulo")
URL = "/churn/auditoria/"
_seq = 0

# Ids de motivo do IXC (ver _IXC_MOTIVO_MAP).
INADIMPLENTE = "6"
POR_OPCAO = "9"
OPERADORA = "25"
TECNICO = "27"
TITULARIDADE = "30"
CIDADE = "31"


def _cancelado(
    org: Organization,
    *,
    em: date,
    motivo: str,
    obs: str = "",
    mrr: str = "100",
    bloqueio_auto: str = "0000-00-00",
    bloqueio_manual: str = "0000-00-00",
    ativado: bool = True,
) -> Contract:
    global _seq
    _seq += 1
    set_current_organization(org)
    return Contract.objects.create(
        organization=org,
        source_type="FAKE",
        external_id=f"aud-{_seq}",
        customer_external_id=f"aud-cli-{_seq}",
        plan_name="Fibra 500",
        monthly_amount=Decimal(mrr),
        status=Contract.Status.CANCELED,
        activated_at=datetime(2025, 1, 10, tzinfo=TZ) if ativado else None,
        # O IXC grava a data sem hora: meia-noite em Brasília.
        canceled_at=datetime.combine(em, time.min, tzinfo=TZ),
        raw_extras={
            "motivo_cancelamento": motivo,
            "obs_cancelamento": obs,
            "dt_ult_bloq_auto": bloqueio_auto,
            "dt_ult_bloq_manual": bloqueio_manual,
        },
    )


MARCO = (date(2026, 3, 1), date(2026, 3, 31))
JUNHO = (date(2026, 6, 1), date(2026, 6, 30))


@pytest.mark.django_db
class TestJanelaPelaCompetencia:
    def test_inadimplente_pesa_no_mes_do_bloqueio_mais_30(
        self, organization_a: Organization
    ) -> None:
        # Bloqueado em 01/03, baixado só em 20/06: o churn é de março.
        _cancelado(
            organization_a, em=date(2026, 6, 20), motivo=INADIMPLENTE,
            bloqueio_auto="2026-03-01",
        )
        em_marco = auditar_cancelamentos(organization_a, inicio=MARCO[0], fim=MARCO[1])
        em_junho = auditar_cancelamentos(organization_a, inicio=JUNHO[0], fim=JUNHO[1])

        assert len(em_marco) == 1
        assert em_junho == []
        r = em_marco[0]
        assert r.veredito.causa_raiz == INADIMPLENCIA
        assert r.competencia.data == date(2026, 3, 31)
        assert r.competencia.base == date(2026, 3, 1)
        assert r.competencia.regra == REGRA_BLOQUEIO_AUTO
        assert r.competencia_mes == "mar/26"

    def test_outros_motivos_pesam_na_data_do_cancelamento(
        self, organization_a: Organization
    ) -> None:
        _cancelado(
            organization_a, em=date(2026, 6, 5), motivo=CIDADE,
            obs="se mudou para outra cidade", bloqueio_auto="2026-03-01",
        )
        assert auditar_cancelamentos(organization_a, inicio=MARCO[0], fim=MARCO[1]) == []
        [r] = auditar_cancelamentos(organization_a, inicio=JUNHO[0], fim=JUNHO[1])
        assert r.veredito.causa_raiz == MUD_SEM_COBERTURA
        assert r.cancelado_em == date(2026, 6, 5)

    def test_bloqueio_do_fim_do_mes_anterior_entra_na_janela(
        self, organization_a: Organization
    ) -> None:
        """A busca no banco começa 31 dias antes da janela: bloqueio em 20/05 é junho."""
        _cancelado(
            organization_a, em=date(2026, 5, 25), motivo=INADIMPLENTE,
            bloqueio_manual="2026-05-20",
        )
        [r] = auditar_cancelamentos(organization_a, inicio=JUNHO[0], fim=JUNHO[1])
        assert r.competencia.data == date(2026, 6, 19)

    def test_pre_contrato_desistido_nao_entra(self, organization_a: Organization) -> None:
        _cancelado(organization_a, em=date(2026, 6, 5), motivo=POR_OPCAO, ativado=False)
        assert auditar_cancelamentos(organization_a, inicio=JUNHO[0], fim=JUNHO[1]) == []

    def test_uma_organizacao_nao_ve_a_outra(
        self, organization_a: Organization, organization_b: Organization
    ) -> None:
        _cancelado(organization_a, em=date(2026, 6, 5), motivo=POR_OPCAO)
        _cancelado(organization_b, em=date(2026, 6, 6), motivo=POR_OPCAO)
        _cancelado(organization_b, em=date(2026, 6, 7), motivo=CIDADE)

        a = compute_churn_audit(organization_a, inicio=JUNHO[0], fim=JUNHO[1])
        b = compute_churn_audit(organization_b, inicio=JUNHO[0], fim=JUNHO[1])

        assert a["kpis"]["auditados"]["n"] == 1
        assert b["kpis"]["auditados"]["n"] == 2
        ids_a = {r.contrato_id for r in a["registros"]}
        ids_b = {r.contrato_id for r in b["registros"]}
        assert not ids_a & ids_b


@pytest.fixture
def junho(organization_a: Organization) -> None:
    """Um mês com um caso de cada nível."""
    _cancelado(  # deveria: técnico com reincidência
        organization_a, em=date(2026, 6, 2), motivo=TECNICO, mrr="150",
        obs="disse que sempre teve problemas com a internet",
    )
    _cancelado(  # poderia: troca de operadora sem motivo
        organization_a, em=date(2026, 6, 3), motivo=OPERADORA, mrr="100",
        obs="trocou de operadora",
    )
    _cancelado(  # parcial: inadimplência sem bloqueio registrado
        organization_a, em=date(2026, 6, 4), motivo=INADIMPLENTE, mrr="80",
    )
    _cancelado(  # inevitável: cidade sem cobertura
        organization_a, em=date(2026, 6, 5), motivo=CIDADE, mrr="90",
        obs="se mudou para o nordeste",
    )
    _cancelado(  # fora: titularidade
        organization_a, em=date(2026, 6, 6), motivo=TITULARIDADE, mrr="70",
        obs="titularidade",
    )
    _cancelado(  # inconsistente: opção, mas a história é mudança sem cobertura
        organization_a, em=date(2026, 6, 7), motivo=POR_OPCAO, mrr="60",
        obs="cliente se mudou para onde não temos cobertura",
    )


@pytest.mark.django_db
class TestIndicadores:
    def test_kpis_por_nivel(self, organization_a: Organization, junho: None) -> None:
        dados = compute_churn_audit(organization_a, inicio=JUNHO[0], fim=JUNHO[1])
        k = dados["kpis"]
        assert k["auditados"] == {"n": 6, "mrr": 550.0}
        # Titularidade não é perda de cliente.
        assert k["churn_real"] == {"n": 5, "mrr": 480.0}
        assert k["retencao_direta"] == {"n": 2, "mrr": 250.0}
        assert k["parcial"]["n"] == 1
        assert k["inevitavel"] == {"n": 2, "mrr": 150.0}
        assert k["fora"]["n"] == 1
        assert k["inconsistentes"] == 1

    def test_fila_de_prioridade_na_ordem(self, organization_a: Organization, junho: None) -> None:
        dados = compute_churn_audit(organization_a, inicio=JUNHO[0], fim=JUNHO[1])
        assert [n["slug"] for n in dados["niveis"]] == [
            "deveria", "poderia", "parcial", "inevitavel", "fora",
        ]
        assert [n["n"] for n in dados["niveis"]] == [1, 1, 1, 2, 1]

    def test_pareto_so_da_retencao_direta(self, organization_a: Organization, junho: None) -> None:
        dados = compute_churn_audit(organization_a, inicio=JUNHO[0], fim=JUNHO[1])
        causas = {b["label"] for b in dados["pareto"]}
        assert INADIMPLENCIA not in causas
        assert len(causas) == 2

    def test_evolucao_mensal_sem_o_fora_do_churn(
        self, organization_a: Organization, junho: None
    ) -> None:
        dados = compute_churn_audit(organization_a, inicio=JUNHO[0], fim=JUNHO[1])
        mensal = dados["mensal"]
        assert mensal["labels"] == ["jun/26"]
        assert mensal["totais"] == [6]
        assert mensal["churn_real"] == [5]
        assert Nivel.FORA.value not in {s["nome"] for s in mensal["por_nivel"]}

    def test_filtro_por_nivel(self, organization_a: Organization, junho: None) -> None:
        dados = compute_churn_audit(
            organization_a, inicio=JUNHO[0], fim=JUNHO[1], filtros=Filtros(nivel="inevitavel")
        )
        assert dados["kpis"]["auditados"]["n"] == 2
        assert {r.nivel for r in dados["registros"]} == {Nivel.INEVITAVEL}
        # O seletor continua oferecendo tudo o que a janela tem.
        assert len(dados["opcoes"]["causas"]) == 5

    def test_busca_ignora_acento_e_caixa(self, organization_a: Organization, junho: None) -> None:
        dados = compute_churn_audit(
            organization_a, inicio=JUNHO[0], fim=JUNHO[1], filtros=Filtros(busca="NÃO TEMOS")
        )
        assert dados["kpis"]["auditados"]["n"] == 1

    def test_competencia_fora_da_janela_e_descartada(
        self, organization_a: Organization, junho: None
    ) -> None:
        """Trocar o período mantém a querystring — o filtro velho não pode zerar a página."""
        dados = compute_churn_audit(
            organization_a, inicio=JUNHO[0], fim=JUNHO[1], filtros=Filtros(competencia="2025-01")
        )
        assert dados["filtros"].competencia is None
        assert dados["kpis"]["auditados"]["n"] == 6


@pytest.mark.django_db
@pytest.mark.filterwarnings("ignore:No directory at:UserWarning")
class TestPagina:
    @pytest.fixture
    def recentes(self, organization_a: Organization) -> None:
        hoje = timezone.localdate()
        _cancelado(
            organization_a, em=hoje - timedelta(days=3), motivo=POR_OPCAO,
            obs="cliente se mudou para onde não temos cobertura",
        )
        _cancelado(
            organization_a, em=hoje - timedelta(days=4), motivo=TECNICO,
            obs="entrou em contato várias vezes e não enviamos um técnico no local",
        )

    def test_renderiza_para_o_dono(self, client: Any, user_a: User, recentes: None) -> None:
        client.force_login(user_a)
        resp = client.get(URL)
        assert resp.status_code == 200
        body = resp.content.decode()
        assert "Auditoria de Churn" in body
        assert "Tabela auditável" in body
        assert "Mudança para local sem cobertura" in body
        assert resp.context["kpis"]["auditados"]["n"] == 2
        assert resp.context["kpis"]["inconsistentes"] == 1

    def test_parametros_invalidos_sao_ignorados(
        self, client: Any, user_a: User, recentes: None
    ) -> None:
        client.force_login(user_a)
        resp = client.get(f"{URL}?competencia=banana&nivel=talvez&causa=inventada&q=")
        assert resp.status_code == 200
        filtros = resp.context["filtros"]
        assert (filtros.competencia, filtros.nivel, filtros.causa, filtros.busca) == (
            None, None, None, None,
        )
        assert resp.context["kpis"]["auditados"]["n"] == 2

    def test_filtro_de_nivel_aparece_na_faixa(
        self, client: Any, user_a: User, recentes: None
    ) -> None:
        client.force_login(user_a)
        resp = client.get(f"{URL}?nivel=deveria")
        assert resp.context["kpis"]["auditados"]["n"] == 1
        body = resp.content.decode()
        assert "Nível: <strong>deveria ter retido</strong>" in body

    def test_esta_no_menu(self, client: Any, user_a: User) -> None:
        client.force_login(user_a)
        body = client.get(URL).content.decode()
        assert 'href="/churn/auditoria/"' in body

    def test_outra_organizacao_nao_aparece(
        self, client: Any, user_b: User, recentes: None
    ) -> None:
        client.force_login(user_b)
        resp = client.get(URL)
        assert resp.status_code == 200
        assert resp.context["kpis"]["auditados"]["n"] == 0

    def test_tabela_tem_teto_e_avisa(
        self, client: Any, user_a: User, recentes: None, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Os indicadores contam tudo; só a tabela para no teto — e diz isso."""
        from apps.dashboards import views

        monkeypatch.setattr(views, "_AUDITORIA_TABELA_MAX", 1)
        client.force_login(user_a)
        resp = client.get(URL)
        assert len(resp.context["linhas"]) == 1
        assert resp.context["linhas_total"] == 2
        assert resp.context["kpis"]["auditados"]["n"] == 2
        assert "Mostrando os 1 mais recentes" in resp.content.decode()


@pytest.mark.django_db
class TestPizzasMensais:
    def test_uma_pizza_por_mes_na_ordem_da_janela(
        self, organization_a: Organization, junho: None
    ) -> None:
        from apps.dashboards.charts import churn_audit_pizzas

        _cancelado(organization_a, em=date(2026, 5, 10), motivo=POR_OPCAO, obs="motivos pessoais")
        dados = compute_churn_audit(organization_a, inicio=date(2026, 4, 1), fim=JUNHO[1])
        visao = churn_audit_pizzas(dados["mensal"])

        assert [(p["label"], p["total"]) for p in visao["pizzas"]] == [
            ("abr/26", 0), ("mai/26", 1), ("jun/26", 6),
        ]
        # Legenda única, só com os motivos que aparecem, na ordem fixa das cores.
        assert [item["nome"] for item in visao["legenda"]] == [
            "Inadimplência", "Mudou de operadora", "Desconexão por opção",
            "Mudou de cidade", "Problemas técnicos", "Troca de titularidade",
        ]

    def test_so_os_ultimos_seis_meses(self, organization_a: Organization, junho: None) -> None:
        from apps.dashboards.charts import churn_audit_pizzas

        dados = compute_churn_audit(organization_a, inicio=date(2025, 10, 1), fim=JUNHO[1])
        visao = churn_audit_pizzas(dados["mensal"])
        assert [p["label"] for p in visao["pizzas"]] == [
            "jan/26", "fev/26", "mar/26", "abr/26", "mai/26", "jun/26",
        ]

    def test_recorte_de_competencia_mostra_so_o_mes(
        self, organization_a: Organization, junho: None
    ) -> None:
        from apps.dashboards.charts import churn_audit_pizzas

        dados = compute_churn_audit(organization_a, inicio=date(2026, 4, 1), fim=JUNHO[1])
        visao = churn_audit_pizzas(dados["mensal"], so_mes="2026-06")
        assert [p["label"] for p in visao["pizzas"]] == ["jun/26"]
