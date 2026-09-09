"""Testes da topologia de rede (#142).

Cobre a ACL Pydantic contra os payloads reais dos cinco endpoints de planta do
IXC, o upsert idempotente do repository, o isolamento entre tenants, o sync
ponta a ponta com o Fake adapter e a leitura de CTOs a partir do banco (que
substituiu a chamada HTTP dentro do render).
"""

from __future__ import annotations

import pytest

from apps.analytics.application.aggregations import compute_cto_summary
from apps.integrations.fake.network_elements import FakeNetworkElementSource
from apps.integrations.ixc.network_elements import IxcNetworkElementSource
from apps.integrations.ixc.schemas import (
    IxcCaixaFtthSchema,
    IxcDfElementoSchema,
    IxcPortaPonSchema,
    IxcRadPopRadioSchema,
    IxcRadPopSchema,
)
from apps.integrations.shared.enums import Capability, SourceType
from apps.integrations.shared.registry import registry
from apps.network.domain.dto import ConnectionDTO, NetworkElementDTO
from apps.network.domain.ports import NetworkElementSourcePort
from apps.network.infrastructure.models import Connection, NetworkElement
from apps.network.infrastructure.repositories import (
    ConnectionRepository,
    NetworkElementRepository,
)
from apps.shared.context import set_current_organization
from apps.sync.models import SyncMode
from apps.sync.tasks import sync_capability
from apps.tenancy.models import Organization, OrganizationDataSource

# =============================================================================
# Payloads reais, copiados da API de produção em 2026-09-08 (sem PII).
# =============================================================================
RAW_CAIXA_FTTH = {
    "codigo_estilo_caixa": "1Pr4jX4X", "id": "1606", "status": "A",
    "id_transmissor": "2", "id_interface": "0", "descricao": "CTO AV-SJOAO ",
    "id_projeto": "7", "id_tecnologia": "0", "capacidade": "8",
    "latitude": "-23.54215200653725", "longitude": "-47.45837508403606",
    "obs_caixa_ftth": "", "cep": "18110210",
    "endereco": "Av. São João, 1064 - Jardim Icatu, Votorantim - SP",
    "numero": "1064", "bairro": "Jardim Icatu", "id_cidade": "3906",
    "ultima_atualizacao": "0000-00-00 00:00:00", "idx": "0", "tipo": "P",
}

RAW_RADPOP = {
    "tp_estacao": "P", "sn_enlace_proprio": "S", "sn_enlace_contratado": "S",
    "sn_enlace_satelite": "S", "id": "14", "pop": "Portal do Pirapora",
    "id_projeto": "7", "endereco": "", "numero": "", "id_cidade": "3906",
    "observacoes": "", "cep": "", "id_fornecedor": "0", "bairro": "",
    "latitude": "-23.65522240656321", "longitude": "-47.66720479335792",
    "abertura": "R", "numestacao_anatel": "0",
}

RAW_RADPOP_RADIO = {
    "id": "3", "ativo": "S", "descricao": "OLT HUAWEI SOROCABA CAJURU DO SUL",
    "id_pop": "11", "ip": "10.55.35.2", "login": "sgpadmin",
    "senha": "a4aXJuJh/FIcwcrDHFls/q/CQc3f==",
    "senha_hw": "8b10achyRmrbG8iHGS9oy2XJw3e6==",
    "modelo": "MA5800-X2", "fabricante_modelo": "HW",
}

RAW_PORTA_PON = {
    "id": "449", "id_pop_radio": "3", "id_slot": "19", "numero_pon": "15",
    "interface": "0/2/15", "vlan_uplink": "100", "potencia_pon": "4.00",
    "potencia_limite": "-28.00", "quantidade_onus": "0",
    "quantidade_onus_autorizadas": "0", "chwidth": "", "canal": "",
}

RAW_DF_ELEMENTO = {
    "id": "680", "descricao": "48 FO - 560 M", "preco_unidade": "0.000000000",
    "observacao": "", "id_tipo_elemento": "69", "id_projeto": "1",
    "tipo": "CB", "id_diretorio": "1", "ultima_atualizacao": "2025-06-10 16:37:21",
}


# =============================================================================
# ACL — validação Pydantic contra o payload real de cada endpoint
# =============================================================================
class TestSchemasIxc:
    def test_caixa_ftth(self) -> None:
        schema = IxcCaixaFtthSchema.model_validate(RAW_CAIXA_FTTH)
        assert schema.id == "1606"
        assert schema.capacidade == 8
        assert schema.latitude == pytest.approx(-23.54215200653725)
        assert schema.id_transmissor == "2"
        # "0" no IXC significa "sem vínculo" — não pode virar o elemento de id 0.
        assert schema.id_interface == ""

    def test_radpop(self) -> None:
        schema = IxcRadPopSchema.model_validate(RAW_RADPOP)
        assert schema.pop == "Portal do Pirapora"
        assert schema.longitude == pytest.approx(-47.66720479335792)

    def test_radpop_radio_nao_guarda_senha(self) -> None:
        schema = IxcRadPopRadioSchema.model_validate(RAW_RADPOP_RADIO)
        assert schema.id_pop == "11"
        # `extra="ignore"`: senha de gerência da OLT não entra no dashboard.
        assert schema.model_extra in (None, {})
        dto = IxcNetworkElementSource._olt_to_dto(schema)
        assert "senha" not in dto.raw_extras
        assert "senha_hw" not in dto.raw_extras

    def test_porta_pon(self) -> None:
        schema = IxcPortaPonSchema.model_validate(RAW_PORTA_PON)
        assert schema.interface == "0/2/15"
        assert schema.id_pop_radio == "3"

    def test_df_elemento(self) -> None:
        schema = IxcDfElementoSchema.model_validate(RAW_DF_ELEMENTO)
        assert schema.tipo == "CB"
        assert schema.ultima_atualizacao is not None

    def test_coordenada_zerada_vira_none(self) -> None:
        schema = IxcCaixaFtthSchema.model_validate(
            {**RAW_CAIXA_FTTH, "latitude": "0", "longitude": ""}
        )
        assert schema.latitude is None
        assert schema.longitude is None


# =============================================================================
# Tradução schema → DTO
# =============================================================================
class TestTraducaoParaDTO:
    def test_cto_pendura_na_olt(self) -> None:
        dto = IxcNetworkElementSource._cto_to_dto(
            IxcCaixaFtthSchema.model_validate(RAW_CAIXA_FTTH)
        )
        assert dto.kind == "CTO"
        assert dto.parent_kind == "OLT"
        assert dto.parent_external_id == "2"
        assert dto.capacity == 8
        assert dto.address.startswith("Av. São João")
        assert dto.raw_extras["bairro"] == "Jardim Icatu"

    def test_pon_pendura_na_olt(self) -> None:
        dto = IxcNetworkElementSource._pon_to_dto(
            IxcPortaPonSchema.model_validate(RAW_PORTA_PON)
        )
        assert (dto.kind, dto.parent_kind, dto.parent_external_id) == (
            "PON", "OLT", "3",
        )
        assert dto.name == "0/2/15"

    def test_olt_pendura_no_pop(self) -> None:
        dto = IxcNetworkElementSource._olt_to_dto(
            IxcRadPopRadioSchema.model_validate(RAW_RADPOP_RADIO)
        )
        assert (dto.kind, dto.parent_kind, dto.parent_external_id) == (
            "OLT", "POP", "11",
        )

    def test_cabo_nao_tem_geometria(self) -> None:
        dto = IxcNetworkElementSource._cable_to_dto(
            IxcDfElementoSchema.model_validate(RAW_DF_ELEMENTO)
        )
        assert dto.kind == "CABLE"
        assert dto.has_position is False

    def test_kind_invalido_e_rejeitado(self) -> None:
        with pytest.raises(ValueError, match="kind inválido"):
            NetworkElementDTO(external_id="1", kind="POSTE")

    def test_adapters_satisfazem_o_port(self) -> None:
        assert isinstance(FakeNetworkElementSource(), NetworkElementSourcePort)
        assert isinstance(
            IxcNetworkElementSource(base_url="http://x", user_id="1", api_token="t"),
            NetworkElementSourcePort,
        )


# =============================================================================
# Repository — upsert idempotente e isolamento entre tenants
# =============================================================================
@pytest.mark.django_db
class TestNetworkElementRepository:
    def _dto(self, **over: object) -> NetworkElementDTO:
        base: dict = {
            "external_id": "1606", "kind": "CTO", "name": "CTO AV-SJOAO",
            "capacity": 8, "parent_external_id": "2", "parent_kind": "OLT",
        }
        base.update(over)
        return NetworkElementDTO(**base)  # type: ignore[arg-type]

    def test_cria_elemento(self, organization_a: Organization) -> None:
        set_current_organization(organization_a)
        element, created = NetworkElementRepository(organization_a).upsert_from_dto(
            self._dto(), source_type=SourceType.IXC
        )
        assert created is True
        assert element.kind == "CTO"
        assert element.capacity == 8

    def test_upsert_idempotente(self, organization_a: Organization) -> None:
        set_current_organization(organization_a)
        repo = NetworkElementRepository(organization_a)
        repo.upsert_from_dto(self._dto(), source_type=SourceType.IXC)
        _, created = repo.upsert_from_dto(
            self._dto(name="CTO AV-SJOAO (recadastrada)"),
            source_type=SourceType.IXC,
        )
        assert created is False

        set_current_organization(organization_a)
        assert NetworkElement.objects.count() == 1
        assert NetworkElement.objects.get().name == "CTO AV-SJOAO (recadastrada)"

    def test_mesmo_id_em_kinds_diferentes_coexiste(
        self, organization_a: Organization
    ) -> None:
        """Ids do IXC são sequência por tabela: a CTO 12 não é o POP 12."""
        set_current_organization(organization_a)
        repo = NetworkElementRepository(organization_a)
        repo.upsert_from_dto(
            self._dto(external_id="12", kind="CTO"), source_type=SourceType.IXC
        )
        repo.upsert_from_dto(
            self._dto(external_id="12", kind="POP", capacity=None),
            source_type=SourceType.IXC,
        )
        set_current_organization(organization_a)
        assert NetworkElement.objects.filter(external_id="12").count() == 2

    def test_sem_vazamento_entre_tenants(
        self, organization_a: Organization, organization_b: Organization
    ) -> None:
        set_current_organization(organization_a)
        NetworkElementRepository(organization_a).upsert_from_dto(
            self._dto(), source_type=SourceType.IXC
        )
        set_current_organization(organization_b)
        NetworkElementRepository(organization_b).upsert_from_dto(
            self._dto(external_id="9999", name="CTO da outra org"),
            source_type=SourceType.IXC,
        )

        set_current_organization(organization_a)
        assert list(
            NetworkElement.objects.values_list("external_id", flat=True)
        ) == ["1606"]

        set_current_organization(organization_b)
        assert list(
            NetworkElement.objects.values_list("external_id", flat=True)
        ) == ["9999"]


# =============================================================================
# Sync ponta a ponta pelo registry, com o Fake adapter
# =============================================================================
@pytest.mark.django_db
@pytest.mark.e2e
class TestSyncTopologia:
    def test_capability_registrada(self) -> None:
        assert (
            registry.get_factory(SourceType.FAKE, Capability.NETWORK_ELEMENTS)
            is FakeNetworkElementSource
        )
        assert (
            registry.get_factory(SourceType.IXC, Capability.NETWORK_ELEMENTS)
            is IxcNetworkElementSource
        )

    def test_sync_persiste_a_planta(
        self,
        organization_a: Organization,
        datasource_fake_network_elements_a: OrganizationDataSource,
        sample_network_element_dtos: list[NetworkElementDTO],
    ) -> None:
        FakeNetworkElementSource.set_seed(sample_network_element_dtos)

        resultado = sync_capability(
            organization_id=organization_a.pk,
            capability=Capability.NETWORK_ELEMENTS.value,
            mode=SyncMode.BOOTSTRAP.value,
        )
        assert resultado["records_processed"] == len(sample_network_element_dtos)

        set_current_organization(organization_a)
        assert NetworkElement.objects.count() == len(sample_network_element_dtos)
        assert NetworkElement.objects.filter(kind="CTO").count() == 2
        pon = NetworkElement.objects.get(kind="PON")
        assert (pon.parent_kind, pon.parent_external_id) == ("OLT", "3")

    def test_rerodar_sync_nao_duplica(
        self,
        organization_a: Organization,
        datasource_fake_network_elements_a: OrganizationDataSource,
        sample_network_element_dtos: list[NetworkElementDTO],
    ) -> None:
        FakeNetworkElementSource.set_seed(sample_network_element_dtos)
        for _ in range(2):
            sync_capability(
                organization_id=organization_a.pk,
                capability=Capability.NETWORK_ELEMENTS.value,
                mode=SyncMode.BOOTSTRAP.value,
            )
        set_current_organization(organization_a)
        assert NetworkElement.objects.count() == len(sample_network_element_dtos)


# =============================================================================
# compute_cto_summary lê do banco, não do IXC (#142)
# =============================================================================
@pytest.mark.django_db
class TestCtoSummaryLeDoBanco:
    def _planta(
        self, org: Organization, dtos: list[NetworkElementDTO]
    ) -> None:
        set_current_organization(org)
        repo = NetworkElementRepository(org)
        for dto in dtos:
            repo.upsert_from_dto(dto, source_type=SourceType.IXC)

    def _conexao(self, org: Organization, external_id: str, cto: str) -> None:
        set_current_organization(org)
        ConnectionRepository(org).upsert_from_dto(
            ConnectionDTO(
                external_id=external_id,
                customer_external_id="c1",
                contract_external_id="ct1",
                login=f"login{external_id}",
                status="ONLINE",
                cto_external_id=cto,
            ),
            source_type=SourceType.IXC,
        )

    def test_ocupacao_por_projeto(
        self,
        organization_a: Organization,
        sample_network_element_dtos: list[NetworkElementDTO],
    ) -> None:
        self._planta(organization_a, sample_network_element_dtos)
        self._conexao(organization_a, "1", "1606")
        self._conexao(organization_a, "2", "1606")
        self._conexao(organization_a, "3", "1607")

        resumo = compute_cto_summary(organization_a)

        # Só as CTOs entram no resumo — POP, OLT, PON e cabo ficam de fora.
        assert resumo["total_ctos"] == 2
        assert resumo["total_ports"] == 24  # 8 + 16
        assert resumo["total_occupied"] == 3
        assert resumo["total_free"] == 21
        assert resumo["by_project"][0]["project_id"] == "7"
        top = {c["cto_id"]: c for c in resumo["top_ctos"]}
        assert top["1606"]["occupancy_pct"] == 25.0
        assert top["1606"]["bairro"] == "Jardim Icatu"

    def test_nao_toca_no_ixc(
        self,
        organization_a: Organization,
        sample_network_element_dtos: list[NetworkElementDTO],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A regressão que motivou o #142: HTTP pro IXC no meio do render."""
        import apps.integrations.ixc.client as ixc_client

        def _explode(*_args: object, **_kwargs: object) -> None:
            raise AssertionError("compute_cto_summary não pode chamar o IXC")

        monkeypatch.setattr(ixc_client.IxcHttpClient, "__init__", _explode)
        self._planta(organization_a, sample_network_element_dtos)

        resumo = compute_cto_summary(organization_a)
        assert resumo["total_ctos"] == 2

    def test_resumo_isolado_por_tenant(
        self,
        organization_a: Organization,
        organization_b: Organization,
        sample_network_element_dtos: list[NetworkElementDTO],
    ) -> None:
        self._planta(organization_a, sample_network_element_dtos)
        assert compute_cto_summary(organization_b)["total_ctos"] == 0
        assert compute_cto_summary(organization_a)["total_ctos"] == 2

    def test_conexao_sem_cto_nao_conta(
        self,
        organization_a: Organization,
        sample_network_element_dtos: list[NetworkElementDTO],
    ) -> None:
        self._planta(organization_a, sample_network_element_dtos)
        self._conexao(organization_a, "9", "")
        set_current_organization(organization_a)
        assert Connection.objects.count() == 1
        assert compute_cto_summary(organization_a)["total_occupied"] == 0
