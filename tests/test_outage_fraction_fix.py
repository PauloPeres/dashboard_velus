"""Correção das frações impossíveis já gravadas (`fix_outage_fractions`).

A massiva encerrada é o registro que dura — ninguém a recalcula depois. Então a
correção do detector não limpa o que já está no banco: em produção ficaram
quatro eventos com fração acima de 100% (PON 364 com 2,25, PON 385 com 1,29 e
dois GEO com 1,14), e é isso que a tela mostraria para sempre no histórico.

O que estes testes travam é o limite do comando: ele conserta o que é
demonstravelmente impossível e **não encosta** no resto, inclusive quando o
cadastro de hoje não sustenta nenhum número.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from django.core.management import call_command

from apps.customers.infrastructure.models import Contract
from apps.network.infrastructure.models import (
    Connection,
    ConnectionDropEvent,
    OutageAffectedLogin,
    OutageEvent,
)
from apps.shared.context import set_current_organization
from apps.tenancy.models import Organization

AGORA = datetime(2026, 9, 18, 11, 43, tzinfo=UTC)


def _conexao(
    org: Organization, *, login: str, pon: str = "", cto: str = "", contrato: str = ""
) -> Connection:
    return Connection.objects.create(
        organization=org,
        source_type="IXC",
        external_id=f"conn-{login}",
        customer_external_id=f"cust-{login}",
        contract_external_id=contrato,
        login=login,
        pon_external_id=pon,
        cto_external_id=cto,
        status=Connection.Status.ONLINE,
    )


def _massiva(
    org: Organization,
    *,
    scope: str,
    element_id: str,
    fracao: float,
    afetados: list[Connection],
) -> OutageEvent:
    outage = OutageEvent.objects.create(
        organization=org,
        started_at=AGORA,
        ended_at=AGORA + timedelta(hours=1),
        last_detected_at=AGORA,
        scope=scope,
        element_external_id=element_id,
        element_label=f"{scope} {element_id}".strip(),
        confidence=OutageEvent.Confidence.MEDIUM,
        affected_count=len(afetados),
        restored_count=len(afetados),
        affected_fraction=fracao,
    )
    for conexao in afetados:
        queda = ConnectionDropEvent.objects.create(
            organization=org,
            connection=conexao,
            login=conexao.login,
            dropped_at=AGORA,
            restored_at=AGORA + timedelta(hours=1),
            cto_external_id=conexao.cto_external_id,
            monthly_amount=Decimal("99.90"),
        )
        OutageAffectedLogin.objects.create(
            organization=org,
            outage=outage,
            drop_event=queda,
            login=conexao.login,
            dropped_at=queda.dropped_at,
            restored_at=queda.restored_at,
        )
    return outage


@pytest.mark.django_db
class TestFixOutageFractions:
    def test_pon_volta_a_ter_o_denominador_da_porta(
        self, organization_a: Organization
    ) -> None:
        """A PON 364 de produção: 31 afetados sobre uma porta de 67 logins — o
        que o valor gravado (2,25) dizia era que caíram duas portas e um quarto."""
        set_current_organization(organization_a)
        afetados = [
            _conexao(organization_a, login=f"a{i}", pon="364", cto=f"CTO-{i % 3}")
            for i in range(6)
        ]
        # Mais quatro logins da mesma porta que não caíram: denominador 10.
        for i in range(4):
            _conexao(organization_a, login=f"ok{i}", pon="364", cto="CTO-9")
        outage = _massiva(
            organization_a,
            scope=OutageEvent.Scope.PON,
            element_id="364",
            fracao=2.25,
            afetados=afetados,
        )

        call_command("fix_outage_fractions", "acme")

        outage.refresh_from_db()
        assert outage.affected_fraction == pytest.approx(0.6)

    def test_fracao_plausivel_nao_e_tocada(
        self, organization_a: Organization
    ) -> None:
        """O valor gravado é o pico medido durante o evento; o recálculo usa o
        cadastro de hoje. Onde o número antigo não é impossível, mexer nele
        trocaria uma medida de verdade por uma reconstrução."""
        set_current_organization(organization_a)
        afetados = [
            _conexao(organization_a, login=f"b{i}", pon="500", cto="CTO-5")
            for i in range(5)
        ]
        outage = _massiva(
            organization_a,
            scope=OutageEvent.Scope.PON,
            element_id="500",
            fracao=0.43,
            afetados=afetados,
        )

        call_command("fix_outage_fractions", "acme")

        outage.refresh_from_db()
        assert outage.affected_fraction == pytest.approx(0.43)

    def test_sem_denominador_no_cadastro_o_valor_fica_como_estava(
        self, organization_a: Organization
    ) -> None:
        """Sem logins na porta hoje, não há número honesto a escrever — e
        inventar um seria pior que deixar o errado, que ao menos é reconhecível
        como impossível."""
        set_current_organization(organization_a)
        afetados = [
            _conexao(organization_a, login=f"c{i}", pon="", cto="") for i in range(5)
        ]
        outage = _massiva(
            organization_a,
            scope=OutageEvent.Scope.PON,
            element_id="999",
            fracao=1.8,
            afetados=afetados,
        )

        call_command("fix_outage_fractions", "acme")

        outage.refresh_from_db()
        assert outage.affected_fraction == pytest.approx(1.8)

    def test_contrato_cancelado_sai_do_denominador(
        self, organization_a: Organization
    ) -> None:
        """Mesmo denominador do detector: login de contrato cancelado não conta,
        senão a porta pareceria maior do que é e a fração, menor."""
        set_current_organization(organization_a)
        Contract.objects.create(
            organization=organization_a,
            source_type="IXC",
            external_id="ctr-cancelado",
            customer_external_id="cust-x",
            plan_name="Fibra 500MB",
            monthly_amount=Decimal("99.90"),
            status=Contract.Status.CANCELED,
        )
        afetados = [
            _conexao(organization_a, login=f"d{i}", pon="700", cto="CTO-7")
            for i in range(4)
        ]
        _conexao(organization_a, login="ativo", pon="700", cto="CTO-7")
        _conexao(
            organization_a, login="cancelado", pon="700", cto="CTO-7",
            contrato="ctr-cancelado",
        )
        outage = _massiva(
            organization_a,
            scope=OutageEvent.Scope.PON,
            element_id="700",
            fracao=1.4,
            afetados=afetados,
        )

        call_command("fix_outage_fractions", "acme")

        outage.refresh_from_db()
        # 4 de 5 logins ativos na porta — não 4 de 6.
        assert outage.affected_fraction == pytest.approx(0.8)

    def test_dry_run_nao_grava(self, organization_a: Organization) -> None:
        set_current_organization(organization_a)
        afetados = [
            _conexao(organization_a, login=f"e{i}", pon="800", cto="CTO-8")
            for i in range(5)
        ]
        for i in range(5):
            _conexao(organization_a, login=f"eok{i}", pon="800", cto="CTO-8")
        outage = _massiva(
            organization_a,
            scope=OutageEvent.Scope.PON,
            element_id="800",
            fracao=1.9,
            afetados=afetados,
        )

        call_command("fix_outage_fractions", "acme", "--dry-run")

        outage.refresh_from_db()
        assert outage.affected_fraction == pytest.approx(1.9)
