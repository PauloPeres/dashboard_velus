"""Telefone e botão de WhatsApp na lista de atendimentos.

Pedido do Paulo em 20/09/2026, e o desenho mudou por causa de uma pergunta dele:
*"esses dados vêm do IXC ou do Opa?"*

Vêm dos dois — e são **dois números diferentes**:

- o **da conversa** (`canal_cliente`, que o Opa guarda em cada atendimento) é de
  quem realmente falou. Tem WhatsApp por definição: foi por ele que a mensagem
  chegou;
- o **do cadastro** vem do IXC, casado por documento.

*Medido em produção:* nos 1.000 atendimentos mais recentes, os dois **divergem em
236 dos 818** casos em que existem ambos. Por isso o botão usa o número da
conversa — com o do cadastro, um em cada três contatos iria para um número que a
pessoa talvez não use mais.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any

import pytest
from django.utils import timezone

from apps.analytics.application.aggregations import (
    _atendimento_lista_row,
    telefone_do_canal,
)
from apps.atendimento.infrastructure.models import Atendimento
from apps.customers.infrastructure.models import Customer
from apps.shared.context import set_current_organization
from apps.tenancy.models import Organization


def _atendimento(
    org: Organization,
    *,
    canal_cliente: str = "5515991282181@c.us",
    telefone_cadastro: str | None = "(15) 99128-2181",
) -> Atendimento:
    set_current_organization(org)
    cliente = None
    if telefone_cadastro is not None:
        cliente = Customer.objects.create(
            organization=org, source_type="IXC", external_id="cli-1",
            name="Fulano", phone=telefone_cadastro,
        )
    return Atendimento.objects.create(
        organization=org,
        source_type="OPA",
        external_id="at-1",
        customer=cliente,
        customer_external_id="cli-1",
        customer_name="Fulano",
        status=Atendimento.Status.CLOSED,
        canal="whatsapp",
        opened_at=timezone.now() - timedelta(hours=1),
        raw_extras={"canal_cliente": canal_cliente} if canal_cliente else {},
    )


class TestNumeroDaConversa:
    def test_extrai_do_formato_do_opa(self) -> None:
        assert telefone_do_canal({"canal_cliente": "5515991282181@c.us"}) == "5515991282181"

    def test_lixo_nao_vira_telefone(self) -> None:
        """Um link de WhatsApp com número inválido abre numa conversa que não
        existe — pior que não ter botão."""
        assert telefone_do_canal({"canal_cliente": "123@c.us"}) == ""
        assert telefone_do_canal({}) == ""
        assert telefone_do_canal(None) == ""


@pytest.mark.django_db
class TestLinhaDaLista:
    def test_botao_usa_o_numero_de_quem_falou(
        self, organization_a: Organization
    ) -> None:
        at = _atendimento(organization_a, canal_cliente="5515991501696@c.us",
                          telefone_cadastro="(15) 98825-1772")
        linha = _atendimento_lista_row(
            Atendimento.objects.select_related("customer", "departamento").get(pk=at.pk)
        )
        assert linha["whatsapp_url"] == "https://wa.me/5515991501696"
        assert linha["telefone_conversa"] == "(15) 99150-1696"
        assert linha["telefone_cadastro"] == "(15) 98825-1772"

    def test_divergencia_e_declarada(self, organization_a: Organization) -> None:
        """Caso real de produção: cadastro 15988251772, conversou do
        15991501696. Não é erro a esconder — é informação para o atendente e
        para quem cuida da base."""
        at = _atendimento(organization_a, canal_cliente="5515991501696@c.us",
                          telefone_cadastro="(15) 98825-1772")
        linha = _atendimento_lista_row(
            Atendimento.objects.select_related("customer", "departamento").get(pk=at.pk)
        )
        assert linha["telefone_diverge"] is True

    def test_mesmo_numero_com_ddi_e_formatacao_diferente_nao_diverge(
        self, organization_a: Organization
    ) -> None:
        """"5515991282181" e "(15) 99128-2181" são o mesmo telefone — marcar
        divergência aqui encheria a tela de alarme falso."""
        at = _atendimento(organization_a)
        linha = _atendimento_lista_row(
            Atendimento.objects.select_related("customer", "departamento").get(pk=at.pk)
        )
        assert linha["telefone_diverge"] is False

    def test_sem_cliente_vinculado_a_conversa_ainda_tem_botao(
        self, organization_a: Organization
    ) -> None:
        """Em produção, 172 de 1.000 atendimentos não têm cliente vinculado — e
        justamente neles o número da conversa é o único contato que existe."""
        at = _atendimento(organization_a, telefone_cadastro=None)
        linha = _atendimento_lista_row(
            Atendimento.objects.select_related("customer", "departamento").get(pk=at.pk)
        )
        assert linha["whatsapp_url"].startswith("https://wa.me/")
        assert linha["telefone_cadastro"] == ""
        assert linha["telefone_diverge"] is False

    def test_sem_numero_no_canal_nao_inventa_link(
        self, organization_a: Organization
    ) -> None:
        at = _atendimento(organization_a, canal_cliente="")
        linha = _atendimento_lista_row(
            Atendimento.objects.select_related("customer", "departamento").get(pk=at.pk)
        )
        assert linha["whatsapp_url"] == ""
        assert linha["telefone_conversa"] == ""


@pytest.mark.django_db
@pytest.mark.filterwarnings("ignore:No directory at:UserWarning")
class TestTela:
    def test_lista_mostra_contato_e_link(
        self, client: Any, user_a: Any, organization_a: Organization
    ) -> None:
        _atendimento(organization_a, canal_cliente="5515991501696@c.us",
                     telefone_cadastro="(15) 98825-1772")
        client.force_login(user_a)
        hoje = timezone.now().date()
        url = (
            f"/operations/atendimento-lista/?de={hoje - timedelta(days=1)}"
            f"&ate={hoje}&foco=todos&origem=atendimento"
        )
        html = client.get(url).content.decode()
        assert "https://wa.me/5515991501696" in html
        assert "falou de outro número" in html


@pytest.mark.django_db
@pytest.mark.filterwarnings("ignore:No directory at:UserWarning")
class TestCsv:
    """O telefone no CSV — decisão de produto, tomada explicitamente.

    A proposta inicial era deixar o CSV de fora: na tela é consulta, num arquivo
    que sai do sistema vira lista de contatos que circula. A decisão ficou com
    quem responde pela base, e foi incluir (21/09/2026).

    São duas colunas porque são dois números, e juntá-los numa só faria a
    planilha mentir em quase um a cada três casos.
    """

    def test_csv_traz_as_duas_colunas_de_telefone(
        self, client: Any, user_a: Any, organization_a: Organization
    ) -> None:
        _atendimento(organization_a, canal_cliente="5515991501696@c.us",
                     telefone_cadastro="(15) 98825-1772")
        client.force_login(user_a)
        hoje = timezone.now().date()
        url = (
            f"/operations/atendimento-lista/?de={hoje - timedelta(days=1)}"
            f"&ate={hoje}&foco=todos&origem=atendimento&format=csv"
        )
        corpo = client.get(url).content.decode("utf-8")
        assert "Telefone (conversa);Telefone (cadastro)" in corpo
        assert "(15) 99150-1696;(15) 98825-1772" in corpo

    def test_sem_numero_a_coluna_sai_vazia_e_nao_quebra(
        self, client: Any, user_a: Any, organization_a: Organization
    ) -> None:
        _atendimento(organization_a, canal_cliente="", telefone_cadastro=None)
        client.force_login(user_a)
        hoje = timezone.now().date()
        url = (
            f"/operations/atendimento-lista/?de={hoje - timedelta(days=1)}"
            f"&ate={hoje}&foco=todos&origem=atendimento&format=csv"
        )
        resp = client.get(url)
        assert resp.status_code == 200
        assert "Fulano;;;" in resp.content.decode("utf-8")
