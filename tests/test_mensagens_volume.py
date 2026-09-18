"""Testes de `compute_mensagens_volume` e da página Mensagens & Canais.

Cobre as decisões de recorte que mudam a leitura do número:

- mensagem entra pelo período em que foi ENVIADA, não pelo da abertura da
  conversa (é assim que o canal fatura);
- o percentual de "fora da janela de 24h" tem como base o que a Velus enviou —
  usar o total diluiria o número de custo pela metade;
- a média é por conversa COM mensagem, e `cobertura` diz quanto disso é real;
- motivo é multivalorado: conversa com dois motivos conta nos dois.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from apps.analytics.application.aggregations import compute_mensagens_volume
from apps.atendimento.infrastructure.models import (
    Atendimento,
    CanalComunicacao,
    Departamento,
    Mensagem,
)
from apps.integrations.shared.enums import SourceType
from apps.shared.context import set_current_organization
from apps.tenancy.models import Organization

JANELA_INICIO = datetime(2026, 8, 1, tzinfo=UTC)
JANELA_FIM = datetime(2026, 8, 31, 23, 59, tzinfo=UTC)


def _atendimento(
    org: Organization,
    external_id: str,
    *,
    opened_dia: int = 1,
    departamento: Departamento | None = None,
    motivos: list[str] | None = None,
    origem_tipo: str = "",
    origem_ref: str = "",
) -> Atendimento:
    return Atendimento.objects.create(
        organization=org,
        source_type=SourceType.FAKE.value,
        external_id=external_id,
        status=Atendimento.Status.CLOSED.value,
        opened_at=datetime(2026, 8, opened_dia, 10, 0, tzinfo=UTC),
        departamento=departamento,
        motivos=motivos or [],
        origem_tipo=origem_tipo,
        origem_ref=origem_ref,
    )


def _mensagem(
    org: Organization,
    external_id: str,
    atendimento: Atendimento | None,
    *,
    direction: str = "AGENT",
    tipo: str = "texto",
    canal: str = "canal-0800",
    fora: bool | None = None,
    dia: int = 1,
    mes: int = 8,
    hora: int = 12,
) -> Mensagem:
    return Mensagem.objects.create(
        organization=org,
        source_type=SourceType.FAKE.value,
        external_id=external_id,
        atendimento=atendimento,
        atendimento_external_id=atendimento.external_id if atendimento else "",
        direction=direction,
        tipo=tipo,
        canal_external_id=canal,
        fora_janela_24h=fora,
        sent_at=datetime(2026, mes, dia, hora, 0, tzinfo=UTC),
    )


@pytest.fixture
def cenario(db, organization_a: Organization) -> Organization:
    set_current_organization(organization_a)
    suporte = Departamento.objects.create(
        organization=organization_a,
        source_type=SourceType.FAKE.value,
        external_id="dep-sup",
        nome="Suporte",
    )
    comercial = Departamento.objects.create(
        organization=organization_a,
        source_type=SourceType.FAKE.value,
        external_id="dep-com",
        nome="Comercial",
    )
    CanalComunicacao.objects.create(
        organization=organization_a,
        source_type=SourceType.FAKE.value,
        external_id="canal-0800",
        nome="WhatsApp Cloud 0800",
        canal="Whatsapp",
        integracao="facebook",
        status="A",
    )
    CanalComunicacao.objects.create(
        organization=organization_a,
        source_type=SourceType.FAKE.value,
        external_id="canal-velho",
        nome="WhatsApp antigo",
        canal="Whatsapp",
        integracao="dialog360",
        status="I",
    )

    a1 = _atendimento(
        organization_a, "rota-1", departamento=suporte, motivos=["Sem conexão"]
    )
    a2 = _atendimento(
        organization_a,
        "rota-2",
        departamento=comercial,
        motivos=["Nova assinatura", "Mudança de plano"],
        origem_tipo="anuncioWhatsapp",
        origem_ref="ad-123",
    )

    # rota-1: 3 da Velus (1 fora da janela) + 2 do cliente
    _mensagem(organization_a, "m1", a1, fora=True)
    _mensagem(organization_a, "m2", a1, fora=False)
    _mensagem(organization_a, "m3", a1, tipo="menuInterativo", fora=False)
    _mensagem(organization_a, "m4", a1, direction="CLIENT")
    _mensagem(organization_a, "m5", a1, direction="CLIENT", tipo="midia")
    # rota-2: 1 da Velus + 1 do cliente, em outro canal
    _mensagem(organization_a, "m6", a2, canal="canal-velho", fora=False)
    _mensagem(organization_a, "m7", a2, direction="CLIENT", canal="canal-velho")
    # Fora da janela de tempo: julho não pode entrar em agosto.
    _mensagem(organization_a, "m8", a1, dia=15, mes=7)
    return organization_a


@pytest.mark.django_db
class TestComputeMensagensVolume:
    def test_totals_by_direction_within_window(self, cenario: Organization) -> None:
        data = compute_mensagens_volume(
            cenario, start=JANELA_INICIO, end=JANELA_FIM
        )
        assert data["total"] == 7  # m8 (julho) fica de fora
        assert data["n_agente"] == 4
        assert data["n_cliente"] == 3
        assert data["razao_resposta"] == round(4 / 3, 2)

    def test_fora_janela_pct_uses_sent_by_velus_as_base(
        self, cenario: Organization
    ) -> None:
        """Base é o que a Velus enviou (4), não o total (7)."""
        data = compute_mensagens_volume(
            cenario, start=JANELA_INICIO, end=JANELA_FIM
        )
        assert data["fora_janela"] == 1
        assert data["pct_fora_janela"] == 25.0

    def test_por_canal_resolves_catalog_and_flags_inactive(
        self, cenario: Organization
    ) -> None:
        data = compute_mensagens_volume(
            cenario, start=JANELA_INICIO, end=JANELA_FIM
        )
        por_canal = {c["external_id"]: c for c in data["por_canal"]}
        assert por_canal["canal-0800"]["nome"] == "WhatsApp Cloud 0800"
        assert por_canal["canal-0800"]["ativo"] is True
        assert por_canal["canal-0800"]["n"] == 5
        assert por_canal["canal-velho"]["ativo"] is False

    def test_canal_filter_narrows_everything(self, cenario: Organization) -> None:
        data = compute_mensagens_volume(
            cenario,
            start=JANELA_INICIO,
            end=JANELA_FIM,
            canal_external_id="canal-velho",
        )
        assert data["total"] == 2
        assert data["por_departamento"] == [
            {
                "nome": "Comercial",
                "mensagens": 2,
                "conversas": 1,
                "media": 2.0,
                "pct": 100.0,
            }
        ]

    def test_motivo_counts_conversation_in_each_of_its_motives(
        self, cenario: Organization
    ) -> None:
        data = compute_mensagens_volume(
            cenario, start=JANELA_INICIO, end=JANELA_FIM
        )
        por_motivo = {m["nome"]: m for m in data["por_motivo"]}
        # rota-2 tem 2 mensagens e 2 motivos: cada motivo recebe a conversa inteira.
        assert por_motivo["Nova assinatura"]["mensagens"] == 2
        assert por_motivo["Mudança de plano"]["mensagens"] == 2
        assert por_motivo["Sem conexão"]["mensagens"] == 5

    def test_departamento_split(self, cenario: Organization) -> None:
        data = compute_mensagens_volume(
            cenario, start=JANELA_INICIO, end=JANELA_FIM
        )
        por_depto = {d["nome"]: d for d in data["por_departamento"]}
        assert por_depto["Suporte"]["mensagens"] == 5
        assert por_depto["Suporte"]["media"] == 5.0
        assert por_depto["Comercial"]["mensagens"] == 2

    def test_origem_separates_click_to_whatsapp(self, cenario: Organization) -> None:
        data = compute_mensagens_volume(
            cenario, start=JANELA_INICIO, end=JANELA_FIM
        )
        por_origem = {o["tipo"]: o for o in data["origem"]["por_tipo"]}
        assert por_origem["anuncioWhatsapp"]["conversas"] == 1
        assert por_origem["anuncioWhatsapp"]["mensagens"] == 2
        assert por_origem[""]["conversas"] == 1
        assert data["origem"]["top_anuncios"][0]["nome"] == "ad-123"

    def test_coverage_flags_partial_ingestion(self, cenario: Organization) -> None:
        """Conversa sem mensagem ingerida derruba a cobertura — e a tela avisa."""
        set_current_organization(cenario)
        _atendimento(cenario, "rota-3", opened_dia=5)

        data = compute_mensagens_volume(
            cenario, start=JANELA_INICIO, end=JANELA_FIM
        )

        assert data["cobertura"]["conversas"] == 3
        assert data["cobertura"]["conversas_com_mensagem"] == 2
        assert data["cobertura"]["parcial"] is True
        # A média usa as conversas COM mensagem (2), não as 3 existentes.
        assert data["media_por_conversa"] == 3.5

    def test_serie_has_full_axis_with_zero_buckets(
        self, cenario: Organization
    ) -> None:
        data = compute_mensagens_volume(
            cenario, start=JANELA_INICIO, end=JANELA_FIM, granularity="week"
        )
        serie = data["serie"]
        assert len(serie["labels"]) == len(serie["agente"]) == len(serie["cliente"])
        assert sum(serie["agente"]) == 4
        assert sum(serie["cliente"]) == 3
        # Semanas sem mensagem aparecem como zero em vez de sumir do eixo.
        assert serie["agente"].count(0) > 0

    def test_isolated_by_organization(
        self, cenario: Organization, organization_b: Organization
    ) -> None:
        set_current_organization(organization_b)
        data = compute_mensagens_volume(
            organization_b, start=JANELA_INICIO, end=JANELA_FIM
        )
        assert data["total"] == 0


@pytest.mark.django_db
@pytest.mark.filterwarnings("ignore:No directory at:UserWarning")
class TestMensagensPage:
    def test_renders_for_member(self, client, cenario: Organization) -> None:
        from apps.tenancy.models import OrganizationMembership, User

        user = User.objects.create_user(email="gestor@velus.test", password="x")
        OrganizationMembership.objects.create(
            user=user,
            organization=cenario,
            role=OrganizationMembership.Role.OWNER,
            is_active=True,
        )
        client.force_login(user)

        response = client.get("/operations/mensagens/?de=2026-08-01&ate=2026-08-31")

        assert response.status_code == 200
        assert "Mensagens" in response.content.decode()

    def test_requires_login(self, client, cenario: Organization) -> None:
        response = client.get("/operations/mensagens/")
        assert response.status_code in (301, 302)


class TestIniciativaDaConversa:
    """Quem puxou a conversa — a primeira mensagem, não o volume."""

    def test_split_por_quem_mandou_a_primeira(self, db, organization_a: Organization) -> None:
        set_current_organization(organization_a)
        # inbound: cliente fala primeiro, a Velus responde duas vezes
        inbound = _atendimento(organization_a, "in-1", opened_dia=3)
        _mensagem(organization_a, "i1", inbound, direction="CLIENT", dia=3, hora=8)
        _mensagem(organization_a, "i2", inbound, dia=3, hora=9)
        _mensagem(organization_a, "i3", inbound, dia=3, hora=10)
        # outbound: disparo nosso, cliente responde
        outbound = _atendimento(organization_a, "out-1", opened_dia=4)
        _mensagem(organization_a, "o1", outbound, dia=4, hora=8)
        _mensagem(organization_a, "o2", outbound, direction="CLIENT", dia=4, hora=9)

        data = compute_mensagens_volume(
            organization_a, start=JANELA_INICIO, end=JANELA_FIM
        )

        # Volume diz 3x2 pra Velus; iniciativa diz 1x1. São perguntas diferentes.
        assert data["n_agente"] == 3
        assert data["n_cliente"] == 2
        assert data["iniciativa"]["conversas"] == 2
        assert data["iniciativa"]["n_agente"] == 1
        assert data["iniciativa"]["n_cliente"] == 1
        assert data["iniciativa"]["pct_agente"] == 50.0

    def test_primeira_mensagem_vem_de_antes_da_janela(
        self, db, organization_a: Organization
    ) -> None:
        """Conversa aberta na janela conta pela 1a msg dela, mesmo anterior ao corte.

        Sem isso a resposta da Velus dentro da janela viraria "iniciada por nós".
        """
        set_current_organization(organization_a)
        conversa = _atendimento(organization_a, "borda-1", opened_dia=1)
        _mensagem(organization_a, "b1", conversa, direction="CLIENT", dia=31, mes=7)
        _mensagem(organization_a, "b2", conversa, dia=1, hora=10)

        data = compute_mensagens_volume(
            organization_a, start=JANELA_INICIO, end=JANELA_FIM
        )

        assert data["iniciativa"]["n_cliente"] == 1
        assert data["iniciativa"]["n_agente"] == 0

    def test_conversa_sem_mensagem_ingerida_fica_de_fora(
        self, db, organization_a: Organization
    ) -> None:
        set_current_organization(organization_a)
        _atendimento(organization_a, "vazia-1", opened_dia=2)

        data = compute_mensagens_volume(
            organization_a, start=JANELA_INICIO, end=JANELA_FIM
        )

        assert data["iniciativa"]["conversas"] == 0
        assert data["iniciativa"]["pct_cliente"] == 0

    def test_filtro_de_departamento_recorta_iniciativa(
        self, cenario: Organization
    ) -> None:
        set_current_organization(cenario)
        comercial = Departamento.objects.get(organization=cenario, external_id="dep-com")

        data = compute_mensagens_volume(
            cenario,
            start=JANELA_INICIO,
            end=JANELA_FIM,
            departamento_id=comercial.id,
        )

        assert data["iniciativa"]["conversas"] == 1
        # Cobertura também respeita o recorte: 1 conversa do Comercial, 1 com msg.
        assert data["cobertura"]["conversas"] == 1
        assert data["cobertura"]["pct"] == 100.0
