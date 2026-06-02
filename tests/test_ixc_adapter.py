"""Testes do adapter IXC.

Usa `respx` para mockar HTTPX sem rede real. Cobre:
- Anti-Corruption Layer (Pydantic) rejeita schema inválido
- Mapeamento campo-a-campo de IXC → CustomerDTO
- Paginação multi-página
- Tipos inconsistentes (id como int vs string) são coagidos
- `ativo` S/N traduz para status canônico
- `data_cadastro` vira tz-aware
- raw_extras captura campos não-mapeados
"""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

import pytest
import respx
from httpx import Response

from apps.customers.domain.dto import CustomerDTO
from apps.integrations.ixc.customers import IxcCustomerSource
from apps.integrations.ixc.schemas import IxcCustomerSchema
from apps.integrations.shared.enums import Capability, SourceType
from apps.integrations.shared.exceptions import AdapterContractError

BASE_URL = "https://erp.test.com.br"
# O IxcHttpClient adiciona /webservice/v1/ ao base_url, então os mocks
# devem usar a URL completa que o client efetivamente acessa.
API_URL = f"{BASE_URL}/webservice/v1"


def _sample_ixc_customer(**overrides: Any) -> dict[str, Any]:
    """Fixture inline de um registro `cliente` do IXC com campos canônicos."""
    base = {
        "id": "42",
        "razao": "Bruna Carvalho",
        "cnpj_cpf": "123.456.789-01",
        "email": "bruna@example.com",
        "telefone_celular": "11999999999",
        "ativo": "S",
        "data_cadastro": "2025-01-15 10:30:00",
        # Campo extra desconhecido — deve ir pra raw_extras
        "endereco_completo": "Rua das Flores, 123",
    }
    base.update(overrides)
    return base


# =============================================================================
# Schema Pydantic — Anti-Corruption Layer
# =============================================================================
class TestIxcCustomerSchema:
    def test_parses_canonical_response(self) -> None:
        schema = IxcCustomerSchema.model_validate(_sample_ixc_customer())
        assert schema.id == "42"
        assert schema.razao == "Bruna Carvalho"
        assert schema.is_active is True
        assert schema.data_cadastro is not None
        assert schema.data_cadastro.tzinfo is not None  # tz-aware

    def test_coerces_int_id_to_string(self) -> None:
        schema = IxcCustomerSchema.model_validate(_sample_ixc_customer(id=42))
        assert schema.id == "42"

    def test_empty_email_becomes_none(self) -> None:
        schema = IxcCustomerSchema.model_validate(_sample_ixc_customer(email=""))
        assert schema.email is None

    def test_inactive_status(self) -> None:
        schema = IxcCustomerSchema.model_validate(_sample_ixc_customer(ativo="N"))
        assert schema.is_active is False

    def test_extras_captured(self) -> None:
        schema = IxcCustomerSchema.model_validate(_sample_ixc_customer())
        extras = schema.get_extras()
        assert "endereco_completo" in extras
        assert extras["endereco_completo"] == "Rua das Flores, 123"

    def test_rejects_missing_required(self) -> None:
        with pytest.raises(Exception):  # noqa: B017 — ValidationError
            IxcCustomerSchema.model_validate({"id": "1"})  # falta razao

    def test_handles_zero_date_as_none(self) -> None:
        schema = IxcCustomerSchema.model_validate(
            _sample_ixc_customer(data_cadastro="0000-00-00 00:00:00")
        )
        assert schema.data_cadastro is None


# =============================================================================
# Source — capability/source_type declaração
# =============================================================================
class TestIxcCustomerSourceDeclaration:
    def test_implements_port_contract(self) -> None:
        assert IxcCustomerSource.source_type == SourceType.IXC
        assert IxcCustomerSource.capabilities == frozenset({Capability.CUSTOMERS})


# =============================================================================
# Mock HTTP — list_customers ponta a ponta
# Usa fixture `respx_mock` do pytest-respx em vez de decorator de classe
# =============================================================================
class TestIxcCustomerSourceListCustomers:
    def test_lists_single_page_translates_to_dtos(self, respx_mock: respx.MockRouter) -> None:
        respx_mock.get(f"{API_URL}/cliente").mock(
            return_value=Response(
                200,
                json={
                    "page": "1",
                    "total": "2",
                    "registros": [
                        _sample_ixc_customer(id="1", razao="Cliente Um"),
                        _sample_ixc_customer(id="2", razao="Cliente Dois", ativo="N"),
                    ],
                },
            )
        )

        source = IxcCustomerSource(base_url=BASE_URL, user_id="1", api_token="t")
        dtos = list(source.list_customers())

        assert len(dtos) == 2
        assert all(isinstance(d, CustomerDTO) for d in dtos)
        assert dtos[0].name == "Cliente Um"
        assert dtos[0].status == "ACTIVE"
        assert dtos[0].document == "12345678901"  # CPF normalizado
        assert dtos[1].status == "CANCELED"

    def test_paginates_through_multiple_pages(self, respx_mock: respx.MockRouter) -> None:
        """3 páginas com rp=2: 2 cheias + 1 final menor."""
        page_responses = [
            {
                "page": "1",
                "total": "5",
                "registros": [
                    _sample_ixc_customer(id=str(i), razao=f"Cliente {i}")
                    for i in range(1, 3)  # 2 itens (página cheia)
                ],
            },
            {
                "page": "2",
                "total": "5",
                "registros": [
                    _sample_ixc_customer(id=str(i), razao=f"Cliente {i}")
                    for i in range(3, 5)  # 2 itens (página cheia)
                ],
            },
            {
                "page": "3",
                "total": "5",
                "registros": [
                    _sample_ixc_customer(id="5", razao="Cliente 5")
                ],  # 1 item (última, menor que rp)
            },
        ]

        call_count = {"n": 0}

        def page_handler(request: Any) -> Response:
            i = call_count["n"]
            call_count["n"] += 1
            return Response(200, json=page_responses[i])

        respx_mock.get(f"{API_URL}/cliente").mock(side_effect=page_handler)

        # Pra simular múltiplas páginas, precisamos override do page_size do client.
        # IxcCustomerSource não expõe, então usamos o client direto pra testar paginação.
        from apps.integrations.ixc.client import IxcHttpClient

        with IxcHttpClient(base_url=BASE_URL, user_id="1", api_token="t") as client:
            items = list(client.paginate_ixc("cliente", page_size=2))

        assert call_count["n"] == 3
        assert len(items) == 5

    def test_pydantic_validation_failure_raises_contract_error(
        self, respx_mock: respx.MockRouter
    ) -> None:
        """IXC retorna schema corrompido → AdapterContractError."""
        respx_mock.get(f"{API_URL}/cliente").mock(
            return_value=Response(
                200,
                json={
                    "page": "1",
                    "total": "1",
                    "registros": [{"id": "1"}],  # falta razao
                },
            )
        )
        source = IxcCustomerSource(base_url=BASE_URL, user_id="1", api_token="t")
        with pytest.raises(AdapterContractError):
            list(source.list_customers())

    def test_sends_basic_auth_header(self, respx_mock: respx.MockRouter) -> None:
        route = respx_mock.get(f"{API_URL}/cliente").mock(
            return_value=Response(200, json={"page": "1", "total": "0", "registros": []})
        )
        source = IxcCustomerSource(base_url=BASE_URL, user_id="42", api_token="secret-token")
        list(source.list_customers())

        request = route.calls[0].request
        assert "Authorization" in request.headers
        assert request.headers["Authorization"].startswith("Basic ")

    def test_sends_ixcsoft_listar_header(self, respx_mock: respx.MockRouter) -> None:
        route = respx_mock.get(f"{API_URL}/cliente").mock(
            return_value=Response(200, json={"page": "1", "total": "0", "registros": []})
        )
        source = IxcCustomerSource(base_url=BASE_URL, user_id="1", api_token="t")
        list(source.list_customers())

        request = route.calls[0].request
        assert request.headers.get("ixcsoft") == "listar"

    def test_incremental_does_full_scan(self, respx_mock: respx.MockRouter) -> None:
        """IXC não suporta filtro data_alteracao em cliente — incremental faz full scan."""
        route = respx_mock.get(f"{API_URL}/cliente").mock(
            return_value=Response(200, json={"page": "1", "total": "0", "registros": []})
        )
        source = IxcCustomerSource(base_url=BASE_URL, user_id="1", api_token="t")
        since = datetime(2025, 5, 1, 12, 0, tzinfo=ZoneInfo("UTC"))
        list(source.list_customers(since=since))

        # Mesmo com since, não envia filtro data_alteracao (causaria HTML 200 no IXC)
        body = json.loads(route.calls[0].request.content)
        assert "qtype" not in body, "não deve enviar filtro server-side para cliente"
        assert "oper" not in body


# =============================================================================
# Contract adapter — _to_dto com caches de addon/discount
# =============================================================================
from decimal import Decimal

from apps.customers.domain.dto import ContractDTO
from apps.integrations.ixc.contracts import (
    IxcAddonCache,
    IxcContractSource,
    IxcDiscountSurchargeCache,
)
from apps.integrations.ixc.plans import IxcPlanCache, PlanInfo
from apps.integrations.ixc.schemas import (
    IxcContractSchema,
    IxcContractDiscountSchema,
    IxcContractServiceSchema,
    IxcContractSurchargeSchema,
)


def _sample_ixc_contract(**overrides: Any) -> dict[str, Any]:
    """Fixture inline de um registro `cliente_contrato` do IXC."""
    base = {
        "id": "100",
        "id_cliente": "42",
        "id_vd_contrato": "5",
        "descricao_plano": "Plano 100MB",
        "mensalidade": "99.90",
        "status": "A",
        "status_internet": None,
        "data_ativacao": "2025-01-10 00:00:00",
        "data_cancelamento": None,
        "endereco": "Rua ABC, 123",
    }
    base.update(overrides)
    return base


class _StubPlanCache:
    """Stub de IxcPlanCache que retorna um plano fixo."""

    def get(self, plan_id: str) -> PlanInfo | None:
        return PlanInfo(name="Plano Teste", monthly_amount=Decimal("99.90"))


class _StubAddonCache:
    """Stub de IxcAddonCache que retorna 0."""

    def get_total(self, contract_id: str) -> Decimal:
        return Decimal("0")


class _StubDiscountCache:
    """Stub de IxcDiscountSurchargeCache que retorna 0."""

    def get_total_discounts(self, contract_id: str) -> Decimal:
        return Decimal("0")


class TestIxcContractToDto:
    def test_basic_mapping(self) -> None:
        schema = IxcContractSchema.model_validate(_sample_ixc_contract())
        dto = IxcContractSource._to_dto(
            schema, _StubPlanCache(), _StubAddonCache(), _StubDiscountCache()
        )
        assert isinstance(dto, ContractDTO)
        assert dto.external_id == "100"
        assert dto.customer_external_id == "42"
        assert dto.plan_name == "Plano Teste"
        assert dto.monthly_amount == Decimal("99.90")
        assert dto.monthly_amount_addons == Decimal("0")
        assert dto.monthly_amount_discounts == Decimal("0")
        assert dto.status == "ACTIVE"

    def test_addon_cache_populates_addons(self) -> None:
        class AddonWith50:
            def get_total(self, contract_id: str) -> Decimal:
                return Decimal("50.00") if contract_id == "100" else Decimal("0")

        schema = IxcContractSchema.model_validate(_sample_ixc_contract())
        dto = IxcContractSource._to_dto(
            schema, _StubPlanCache(), AddonWith50(), _StubDiscountCache()
        )
        assert dto.monthly_amount_addons == Decimal("50.00")

    def test_discount_cache_populates_discounts(self) -> None:
        class DiscountWith20:
            def get_total_discounts(self, contract_id: str) -> Decimal:
                return Decimal("20.00") if contract_id == "100" else Decimal("0")

        schema = IxcContractSchema.model_validate(_sample_ixc_contract())
        dto = IxcContractSource._to_dto(
            schema, _StubPlanCache(), _StubAddonCache(), DiscountWith20()
        )
        assert dto.monthly_amount_discounts == Decimal("20.00")

    def test_negative_discount_clamped_to_zero(self) -> None:
        """When surcharges > discounts, net discount is negative — clamped to 0."""

        class NegativeDiscount:
            def get_total_discounts(self, contract_id: str) -> Decimal:
                return Decimal("-15.00")

        schema = IxcContractSchema.model_validate(_sample_ixc_contract())
        dto = IxcContractSource._to_dto(
            schema, _StubPlanCache(), _StubAddonCache(), NegativeDiscount()
        )
        assert dto.monthly_amount_discounts == Decimal("0")

    def test_canceled_status_mapping(self) -> None:
        schema = IxcContractSchema.model_validate(_sample_ixc_contract(status="CA"))
        dto = IxcContractSource._to_dto(
            schema, _StubPlanCache(), _StubAddonCache(), _StubDiscountCache()
        )
        assert dto.status == "CANCELED"

    def test_blocked_status_from_internet(self) -> None:
        schema = IxcContractSchema.model_validate(
            _sample_ixc_contract(status="A", status_internet="CM")
        )
        dto = IxcContractSource._to_dto(
            schema, _StubPlanCache(), _StubAddonCache(), _StubDiscountCache()
        )
        assert dto.status == "BLOCKED"


class TestIxcContractServiceSchema:
    def test_parses_valid_record(self) -> None:
        schema = IxcContractServiceSchema.model_validate({
            "id": "1", "id_contrato": "100", "descricao": "IP Fixo",
            "valor_total": "30.00", "status": "I", "tipo": "S",
        })
        assert schema.id == "1"
        assert schema.valor_total == "30.00"

    def test_coerces_int_ids(self) -> None:
        schema = IxcContractServiceSchema.model_validate({
            "id": 1, "id_contrato": 100, "valor_total": "10",
        })
        assert schema.id == "1"
        assert schema.id_contrato == "100"

    def test_comma_amount(self) -> None:
        schema = IxcContractServiceSchema.model_validate({
            "id": "1", "id_contrato": "100", "valor_total": "30,50",
        })
        assert schema.valor_total == "30.50"


class TestIxcContractDiscountSchema:
    def test_parses_valid_record(self) -> None:
        schema = IxcContractDiscountSchema.model_validate({
            "id": "1", "id_contrato": "100", "descricao": "Fidelidade",
            "valor": "10.00", "percentual": "0", "data_validade": "2026-12-31",
        })
        assert schema.valor == "10.00"
        assert schema.data_validade == "2026-12-31"

    def test_zero_date_becomes_empty(self) -> None:
        schema = IxcContractDiscountSchema.model_validate({
            "id": "1", "id_contrato": "100", "data_validade": "0000-00-00",
        })
        assert schema.data_validade == ""


class TestIxcContractSurchargeSchema:
    def test_parses_valid_record(self) -> None:
        schema = IxcContractSurchargeSchema.model_validate({
            "id": "1", "id_contrato": "100", "descricao": "Taxa extra",
            "valor": "15.00", "data_validade": "",
        })
        assert schema.valor == "15.00"
        assert schema.data_validade == ""


# =============================================================================
# Ticket — schema su_oss_chamado + _to_dto
# =============================================================================
from apps.helpdesk.domain.dto import TicketDTO
from apps.integrations.ixc.schemas import IxcTicketSchema
from apps.integrations.ixc.tickets import IxcTicketSource


def _sample_ixc_ticket(**overrides: Any) -> dict[str, Any]:
    """Fixture inline de um registro `su_oss_chamado` do IXC."""
    base = {
        "id": "500",
        "id_cliente": "42",
        "id_assunto": "10",
        "setor": "Suporte",
        "id_tecnico": "3",
        "status": "A",
        "prioridade": "N",
        "mensagem": "Cliente sem conexão",
        "protocolo": "2025050001",
        "data_abertura": "2025-05-10 09:00:00",
        "data_agenda": None,
        "data_fechamento": None,
        "ultima_atualizacao": "2025-05-10 09:30:00",
        # Campo extra desconhecido — deve ir pra raw_extras
        "origem_endereco": "App",
    }
    base.update(overrides)
    return base


class TestIxcTicketSchema:
    def test_parses_canonical_response(self) -> None:
        schema = IxcTicketSchema.model_validate(_sample_ixc_ticket())
        assert schema.id == "500"
        assert schema.id_cliente == "42"
        assert schema.status == "A"
        assert schema.prioridade == "N"
        assert schema.data_abertura is not None
        assert schema.data_abertura.tzinfo is not None  # tz-aware

    def test_coerces_int_id_to_string(self) -> None:
        schema = IxcTicketSchema.model_validate(_sample_ixc_ticket(id=500, id_cliente=42))
        assert schema.id == "500"
        assert schema.id_cliente == "42"

    def test_zero_date_becomes_none(self) -> None:
        schema = IxcTicketSchema.model_validate(
            _sample_ixc_ticket(data_fechamento="0000-00-00 00:00:00")
        )
        assert schema.data_fechamento is None

    def test_null_message_becomes_empty(self) -> None:
        schema = IxcTicketSchema.model_validate(_sample_ixc_ticket(mensagem="null"))
        assert schema.mensagem == ""

    def test_defaults_when_status_priority_missing(self) -> None:
        raw = _sample_ixc_ticket()
        del raw["status"]
        del raw["prioridade"]
        schema = IxcTicketSchema.model_validate(raw)
        assert schema.status == "A"
        assert schema.prioridade == "N"

    def test_extras_captured(self) -> None:
        schema = IxcTicketSchema.model_validate(_sample_ixc_ticket())
        extras = schema.get_extras()
        assert extras.get("origem_endereco") == "App"

    def test_rejects_missing_id(self) -> None:
        raw = _sample_ixc_ticket()
        del raw["id"]
        with pytest.raises(Exception):  # noqa: B017 — ValidationError
            IxcTicketSchema.model_validate(raw)


class TestIxcTicketSourceDeclaration:
    def test_implements_port_contract(self) -> None:
        assert IxcTicketSource.source_type == SourceType.IXC
        assert IxcTicketSource.capabilities == frozenset({Capability.TICKETS})


class TestIxcTicketToDto:
    def test_basic_mapping(self) -> None:
        schema = IxcTicketSchema.model_validate(_sample_ixc_ticket())
        dto = IxcTicketSource._to_dto(schema)
        assert isinstance(dto, TicketDTO)
        assert dto.external_id == "500"
        assert dto.customer_external_id == "42"
        assert dto.subject_id == "10"
        assert dto.sector == "Suporte"
        assert dto.technician_id == "3"
        assert dto.protocol == "2025050001"
        assert dto.message == "Cliente sem conexão"

    @pytest.mark.parametrize(
        ("ixc_status", "expected"),
        [
            ("AG", "SCHEDULED"),
            ("A", "OPEN"),
            ("EX", "IN_PROGRESS"),
            ("F", "CLOSED"),
            ("EN", "FORWARDED"),
            ("ZZ", "OPEN"),  # desconhecido → fallback OPEN
        ],
    )
    def test_status_mapping(self, ixc_status: str, expected: str) -> None:
        schema = IxcTicketSchema.model_validate(_sample_ixc_ticket(status=ixc_status))
        dto = IxcTicketSource._to_dto(schema)
        assert dto.status == expected

    @pytest.mark.parametrize(
        ("ixc_priority", "expected"),
        [
            ("N", "NORMAL"),
            ("A", "HIGH"),
            ("B", "LOW"),
            ("U", "URGENT"),
            ("ZZ", "NORMAL"),  # desconhecido → fallback NORMAL
        ],
    )
    def test_priority_mapping(self, ixc_priority: str, expected: str) -> None:
        schema = IxcTicketSchema.model_validate(_sample_ixc_ticket(prioridade=ixc_priority))
        dto = IxcTicketSource._to_dto(schema)
        assert dto.priority == expected

    def test_closed_ticket_has_closed_at(self) -> None:
        schema = IxcTicketSchema.model_validate(
            _sample_ixc_ticket(status="F", data_fechamento="2025-05-11 10:00:00")
        )
        dto = IxcTicketSource._to_dto(schema)
        assert dto.status == "CLOSED"
        assert dto.closed_at is not None

    def test_lists_single_page_translates_to_dtos(self, respx_mock: respx.MockRouter) -> None:
        respx_mock.get(f"{API_URL}/su_oss_chamado").mock(
            return_value=Response(
                200,
                json={
                    "page": "1",
                    "total": "2",
                    "registros": [
                        _sample_ixc_ticket(id="1", status="A"),
                        _sample_ixc_ticket(id="2", status="F"),
                    ],
                },
            )
        )
        source = IxcTicketSource(base_url=BASE_URL, user_id="1", api_token="t")
        dtos = list(source.list_tickets())

        assert len(dtos) == 2
        assert all(isinstance(d, TicketDTO) for d in dtos)
        assert dtos[0].status == "OPEN"
        assert dtos[1].status == "CLOSED"

    def test_invalid_record_skipped_not_raised(self, respx_mock: respx.MockRouter) -> None:
        """Registro inválido (sem id) é pulado, não derruba o sync inteiro."""
        respx_mock.get(f"{API_URL}/su_oss_chamado").mock(
            return_value=Response(
                200,
                json={
                    "page": "1",
                    "total": "2",
                    "registros": [
                        {"id_cliente": "42"},  # falta id → pulado
                        _sample_ixc_ticket(id="2"),
                    ],
                },
            )
        )
        source = IxcTicketSource(base_url=BASE_URL, user_id="1", api_token="t")
        dtos = list(source.list_tickets())
        assert len(dtos) == 1
        assert dtos[0].external_id == "2"


# =============================================================================
# Connection — schema radusuarios + _to_dto
# =============================================================================
from apps.integrations.ixc.connections import IxcConnectionSource
from apps.integrations.ixc.schemas import IxcRadUserSchema
from apps.network.domain.dto import ConnectionDTO


def _sample_ixc_raduser(**overrides: Any) -> dict[str, Any]:
    """Fixture inline de um registro `radusuarios` do IXC."""
    base = {
        "id": "700",
        "id_cliente": "42",
        "id_contrato": "88",
        "login": "cliente42",
        "ativo": "S",
        "online": "S",
        "ip": "10.0.0.5",
        "nas_ip": "192.168.1.10",
        "download": "500M",
        "upload": "250M",
        "bytes_recebidos": "1073741824",
        "bytes_enviados": "536870912",
        "ultima_conexao": "2025-05-20 08:00:00",
        # Campo extra desconhecido — deve ir pra raw_extras
        "mac": "AA:BB:CC:DD:EE:FF",
    }
    base.update(overrides)
    return base


class TestIxcRadUserSchema:
    def test_parses_canonical_response(self) -> None:
        schema = IxcRadUserSchema.model_validate(_sample_ixc_raduser())
        assert schema.id == "700"
        assert schema.id_cliente == "42"
        assert schema.login == "cliente42"
        assert schema.is_active is True
        assert schema.is_online is True
        assert schema.ultima_conexao is not None
        assert schema.ultima_conexao.tzinfo is not None  # tz-aware

    def test_coerces_int_id_to_string(self) -> None:
        schema = IxcRadUserSchema.model_validate(
            _sample_ixc_raduser(id=700, id_cliente=42)
        )
        assert schema.id == "700"
        assert schema.id_cliente == "42"

    def test_coerces_bytes_to_int(self) -> None:
        schema = IxcRadUserSchema.model_validate(_sample_ixc_raduser())
        assert schema.bytes_recebidos == 1073741824
        assert schema.bytes_enviados == 536870912

    def test_empty_bytes_becomes_zero(self) -> None:
        schema = IxcRadUserSchema.model_validate(
            _sample_ixc_raduser(bytes_recebidos="", bytes_enviados=None)
        )
        assert schema.bytes_recebidos == 0
        assert schema.bytes_enviados == 0

    def test_zero_date_becomes_none(self) -> None:
        schema = IxcRadUserSchema.model_validate(
            _sample_ixc_raduser(ultima_conexao="0000-00-00 00:00:00")
        )
        assert schema.ultima_conexao is None

    def test_defaults_when_flags_missing(self) -> None:
        raw = _sample_ixc_raduser()
        del raw["ativo"]
        del raw["online"]
        schema = IxcRadUserSchema.model_validate(raw)
        assert schema.ativo == "N"
        assert schema.online == "N"

    def test_extras_captured(self) -> None:
        schema = IxcRadUserSchema.model_validate(_sample_ixc_raduser())
        extras = schema.get_extras()
        assert extras.get("mac") == "AA:BB:CC:DD:EE:FF"

    def test_rejects_missing_id(self) -> None:
        raw = _sample_ixc_raduser()
        del raw["id"]
        with pytest.raises(Exception):  # noqa: B017 — ValidationError
            IxcRadUserSchema.model_validate(raw)


class TestIxcConnectionSourceDeclaration:
    def test_implements_port_contract(self) -> None:
        assert IxcConnectionSource.source_type == SourceType.IXC
        assert IxcConnectionSource.capabilities == frozenset({Capability.CONNECTIONS})


class TestIxcConnectionToDto:
    def test_basic_mapping(self) -> None:
        schema = IxcRadUserSchema.model_validate(_sample_ixc_raduser())
        dto = IxcConnectionSource._to_dto(schema)
        assert isinstance(dto, ConnectionDTO)
        assert dto.external_id == "700"
        assert dto.customer_external_id == "42"
        assert dto.contract_external_id == "88"
        assert dto.login == "cliente42"
        assert dto.rx_bytes == 1073741824
        assert dto.tx_bytes == 536870912

    @pytest.mark.parametrize(
        ("ativo", "online", "expected"),
        [
            ("S", "S", "ONLINE"),
            ("S", "N", "OFFLINE"),
            ("N", "S", "BLOCKED"),  # inativo prevalece
            ("N", "N", "BLOCKED"),
        ],
    )
    def test_status_derivation(self, ativo: str, online: str, expected: str) -> None:
        schema = IxcRadUserSchema.model_validate(
            _sample_ixc_raduser(ativo=ativo, online=online)
        )
        dto = IxcConnectionSource._to_dto(schema)
        assert dto.status == expected

    def test_lists_single_page_translates_to_dtos(self, respx_mock: respx.MockRouter) -> None:
        respx_mock.get(f"{API_URL}/radusuarios").mock(
            return_value=Response(
                200,
                json={
                    "page": "1",
                    "total": "2",
                    "registros": [
                        _sample_ixc_raduser(id="1", ativo="S", online="S"),
                        _sample_ixc_raduser(id="2", ativo="N", online="N"),
                    ],
                },
            )
        )
        source = IxcConnectionSource(base_url=BASE_URL, user_id="1", api_token="t")
        dtos = list(source.list_connections())

        assert len(dtos) == 2
        assert all(isinstance(d, ConnectionDTO) for d in dtos)
        assert dtos[0].status == "ONLINE"
        assert dtos[1].status == "BLOCKED"

    def test_invalid_record_skipped_not_raised(self, respx_mock: respx.MockRouter) -> None:
        """Registro inválido (sem id) é pulado, não derruba o sync inteiro."""
        respx_mock.get(f"{API_URL}/radusuarios").mock(
            return_value=Response(
                200,
                json={
                    "page": "1",
                    "total": "2",
                    "registros": [
                        {"id_cliente": "42"},  # falta id → pulado
                        _sample_ixc_raduser(id="2"),
                    ],
                },
            )
        )
        source = IxcConnectionSource(base_url=BASE_URL, user_id="1", api_token="t")
        dtos = list(source.list_connections())
        assert len(dtos) == 1
        assert dtos[0].external_id == "2"


# =============================================================================
# Payment — schema fn_areceber_baixas + _to_dto
# =============================================================================
from decimal import Decimal

from apps.financial.domain.dto import PaymentDTO
from apps.integrations.ixc.payments import IxcPaymentSource
from apps.integrations.ixc.schemas import IxcPaymentSchema


def _sample_ixc_baixa(**overrides: Any) -> dict[str, Any]:
    """Fixture inline de um registro `fn_areceber_baixas` do IXC."""
    base = {
        "id": "900",
        "id_areceber": "55",
        "id_cliente": "42",
        "valor": "150.00",
        "data_baixa": "2025-05-15 10:30:00",
        "forma_pagamento": "PIX",
        "juros": "5.00",
        "multa": "2.50",
        "desconto": "0.00",
        # Campo extra desconhecido — deve ir pra raw_extras
        "id_operador": "7",
    }
    base.update(overrides)
    return base


class TestIxcPaymentSchema:
    def test_parses_canonical_response(self) -> None:
        schema = IxcPaymentSchema.model_validate(_sample_ixc_baixa())
        assert schema.id == "900"
        assert schema.id_areceber == "55"
        assert schema.valor == "150.00"
        assert schema.data_baixa is not None
        assert schema.data_baixa.tzinfo is not None  # tz-aware

    def test_coerces_int_id_to_string(self) -> None:
        schema = IxcPaymentSchema.model_validate(
            _sample_ixc_baixa(id=900, id_areceber=55)
        )
        assert schema.id == "900"
        assert schema.id_areceber == "55"

    def test_coerces_comma_decimal(self) -> None:
        schema = IxcPaymentSchema.model_validate(_sample_ixc_baixa(valor="150,75"))
        assert schema.valor == "150.75"  # vírgula → ponto

    def test_empty_amounts_become_zero(self) -> None:
        schema = IxcPaymentSchema.model_validate(
            _sample_ixc_baixa(juros="", multa=None, desconto="0.00")
        )
        assert schema.juros == "0"
        assert schema.multa == "0"
        assert schema.desconto == "0"

    def test_zero_date_becomes_none(self) -> None:
        schema = IxcPaymentSchema.model_validate(
            _sample_ixc_baixa(data_baixa="0000-00-00 00:00:00")
        )
        assert schema.data_baixa is None

    def test_extras_captured(self) -> None:
        schema = IxcPaymentSchema.model_validate(_sample_ixc_baixa())
        extras = schema.get_extras()
        assert extras.get("id_operador") == "7"

    def test_rejects_missing_id(self) -> None:
        raw = _sample_ixc_baixa()
        del raw["id"]
        with pytest.raises(Exception):  # noqa: B017 — ValidationError
            IxcPaymentSchema.model_validate(raw)


class TestIxcPaymentSourceDeclaration:
    def test_implements_port_contract(self) -> None:
        assert IxcPaymentSource.source_type == SourceType.IXC
        assert IxcPaymentSource.capabilities == frozenset({Capability.PAYMENTS})


class TestIxcPaymentToDto:
    def test_basic_mapping(self) -> None:
        schema = IxcPaymentSchema.model_validate(_sample_ixc_baixa())
        dto = IxcPaymentSource._to_dto(schema)
        assert isinstance(dto, PaymentDTO)
        assert dto.external_id == "900"
        assert dto.invoice_external_id == "55"
        assert dto.contract_external_id is None
        assert dto.amount == Decimal("150.00")
        assert dto.method == "PIX"

    def test_juros_multa_desconto_in_extras(self) -> None:
        schema = IxcPaymentSchema.model_validate(_sample_ixc_baixa())
        dto = IxcPaymentSource._to_dto(schema)
        assert dto is not None
        assert dto.raw_extras["juros"] == "5.00"
        assert dto.raw_extras["multa"] == "2.50"
        assert dto.raw_extras["desconto"] == "0"

    def test_baixa_without_date_skipped(self) -> None:
        schema = IxcPaymentSchema.model_validate(
            _sample_ixc_baixa(data_baixa="0000-00-00 00:00:00")
        )
        assert IxcPaymentSource._to_dto(schema) is None

    def test_empty_invoice_id_becomes_none(self) -> None:
        schema = IxcPaymentSchema.model_validate(_sample_ixc_baixa(id_areceber=""))
        dto = IxcPaymentSource._to_dto(schema)
        assert dto is not None
        assert dto.invoice_external_id is None

    @pytest.mark.parametrize(
        ("forma", "expected"),
        [
            ("PIX", "PIX"),
            ("Boleto Bancário", "BOLETO"),
            ("Cartão de Crédito", "CARD"),
            ("TED", "TRANSFER"),
            ("Dinheiro", "CASH"),
            ("", "UNKNOWN"),
            ("Cheque", "UNKNOWN"),
        ],
    )
    def test_method_mapping(self, forma: str, expected: str) -> None:
        schema = IxcPaymentSchema.model_validate(_sample_ixc_baixa(forma_pagamento=forma))
        dto = IxcPaymentSource._to_dto(schema)
        assert dto is not None
        assert dto.method == expected

    def test_lists_single_page_translates_to_dtos(self, respx_mock: respx.MockRouter) -> None:
        respx_mock.get(f"{API_URL}/fn_areceber_baixas").mock(
            return_value=Response(
                200,
                json={
                    "page": "1",
                    "total": "2",
                    "registros": [
                        _sample_ixc_baixa(id="1", forma_pagamento="PIX"),
                        _sample_ixc_baixa(id="2", forma_pagamento="Boleto"),
                    ],
                },
            )
        )
        source = IxcPaymentSource(base_url=BASE_URL, user_id="1", api_token="t")
        dtos = list(source.list_payments())

        assert len(dtos) == 2
        assert all(isinstance(d, PaymentDTO) for d in dtos)
        assert dtos[0].method == "PIX"
        assert dtos[1].method == "BOLETO"

    def test_invalid_record_skipped_not_raised(self, respx_mock: respx.MockRouter) -> None:
        """Registro inválido (sem id) é pulado, não derruba o sync inteiro."""
        respx_mock.get(f"{API_URL}/fn_areceber_baixas").mock(
            return_value=Response(
                200,
                json={
                    "page": "1",
                    "total": "2",
                    "registros": [
                        {"id_areceber": "55"},  # falta id → pulado
                        _sample_ixc_baixa(id="2"),
                    ],
                },
            )
        )
        source = IxcPaymentSource(base_url=BASE_URL, user_id="1", api_token="t")
        dtos = list(source.list_payments())
        assert len(dtos) == 1
        assert dtos[0].external_id == "2"


# =============================================================================
# Equipment — schema cliente_contrato_comodato + _to_dto
# =============================================================================
from apps.integrations.ixc.equipment import IxcEquipmentSource
from apps.integrations.ixc.schemas import IxcEquipmentSchema
from apps.inventory.domain.dto import EquipmentDTO


def _sample_ixc_comodato(**overrides: Any) -> dict[str, Any]:
    """Fixture inline de um registro `cliente_contrato_comodato` do IXC."""
    base = {
        "id": "300",
        "id_cliente_contrato": "100",
        "id_produto": "12",
        "descricao": "ONT Huawei HG8245",
        "serial": "SN-0001",
        "mac": "AA:BB:CC:00:00:01",
        "valor": "250.00",
        "status": "A",
        # Campo extra desconhecido — deve ir pra raw_extras
        "id_filial": "1",
    }
    base.update(overrides)
    return base


class TestIxcEquipmentSchema:
    def test_parses_canonical_response(self) -> None:
        schema = IxcEquipmentSchema.model_validate(_sample_ixc_comodato())
        assert schema.id == "300"
        assert schema.id_cliente_contrato == "100"
        assert schema.id_produto == "12"
        assert schema.descricao == "ONT Huawei HG8245"
        assert schema.serial == "SN-0001"
        assert schema.valor == "250.00"
        assert schema.status == "A"

    def test_coerces_int_id_to_string(self) -> None:
        schema = IxcEquipmentSchema.model_validate(
            _sample_ixc_comodato(id=300, id_cliente_contrato=100)
        )
        assert schema.id == "300"
        assert schema.id_cliente_contrato == "100"

    def test_coerces_comma_decimal(self) -> None:
        schema = IxcEquipmentSchema.model_validate(_sample_ixc_comodato(valor="250,75"))
        assert schema.valor == "250.75"  # vírgula → ponto

    def test_empty_valor_becomes_zero(self) -> None:
        schema = IxcEquipmentSchema.model_validate(
            _sample_ixc_comodato(valor="")
        )
        assert schema.valor == "0"

    def test_defaults_when_optional_missing(self) -> None:
        schema = IxcEquipmentSchema.model_validate({"id": "300"})
        assert schema.id_cliente_contrato == ""
        assert schema.descricao == ""
        assert schema.valor == "0"
        assert schema.status == ""

    def test_extras_captured(self) -> None:
        schema = IxcEquipmentSchema.model_validate(_sample_ixc_comodato())
        extras = schema.get_extras()
        assert extras.get("id_filial") == "1"

    def test_rejects_missing_id(self) -> None:
        raw = _sample_ixc_comodato()
        del raw["id"]
        with pytest.raises(Exception):  # noqa: B017 — ValidationError
            IxcEquipmentSchema.model_validate(raw)


class TestIxcEquipmentSourceDeclaration:
    def test_implements_port_contract(self) -> None:
        assert IxcEquipmentSource.source_type == SourceType.IXC
        assert IxcEquipmentSource.capabilities == frozenset({Capability.EQUIPMENT})


class TestIxcEquipmentToDto:
    def test_basic_mapping(self) -> None:
        schema = IxcEquipmentSchema.model_validate(_sample_ixc_comodato())
        dto = IxcEquipmentSource._to_dto(schema)
        assert isinstance(dto, EquipmentDTO)
        assert dto.external_id == "300"
        assert dto.contract_external_id == "100"
        assert dto.product_name == "ONT Huawei HG8245"
        assert dto.serial == "SN-0001"
        assert dto.mac == "AA:BB:CC:00:00:01"
        assert dto.value == Decimal("250.00")

    @pytest.mark.parametrize(
        ("ixc_status", "expected"),
        [
            ("A", "ACTIVE"),
            ("S", "ACTIVE"),
            ("D", "RETURNED"),
            ("N", "RETURNED"),
            ("ZZ", "UNKNOWN"),  # desconhecido → fallback UNKNOWN
            ("", "UNKNOWN"),
        ],
    )
    def test_status_mapping(self, ixc_status: str, expected: str) -> None:
        schema = IxcEquipmentSchema.model_validate(
            _sample_ixc_comodato(status=ixc_status)
        )
        dto = IxcEquipmentSource._to_dto(schema)
        assert dto.status == expected

    def test_product_name_fallback_to_id_produto(self) -> None:
        schema = IxcEquipmentSchema.model_validate(
            _sample_ixc_comodato(descricao="", id_produto="12")
        )
        dto = IxcEquipmentSource._to_dto(schema)
        assert dto.product_name == "Produto #12"

    def test_product_name_empty_when_no_descricao_no_produto(self) -> None:
        schema = IxcEquipmentSchema.model_validate(
            _sample_ixc_comodato(descricao="", id_produto="")
        )
        dto = IxcEquipmentSource._to_dto(schema)
        assert dto.product_name == ""

    def test_lists_single_page_translates_to_dtos(self, respx_mock: respx.MockRouter) -> None:
        respx_mock.get(f"{API_URL}/cliente_contrato_comodato").mock(
            return_value=Response(
                200,
                json={
                    "page": "1",
                    "total": "2",
                    "registros": [
                        _sample_ixc_comodato(id="1", status="A"),
                        _sample_ixc_comodato(id="2", status="D"),
                    ],
                },
            )
        )
        source = IxcEquipmentSource(base_url=BASE_URL, user_id="1", api_token="t")
        dtos = list(source.list_equipment())

        assert len(dtos) == 2
        assert all(isinstance(d, EquipmentDTO) for d in dtos)
        assert dtos[0].status == "ACTIVE"
        assert dtos[1].status == "RETURNED"

    def test_invalid_record_skipped_not_raised(self, respx_mock: respx.MockRouter) -> None:
        """Registro inválido (sem id) é pulado, não derruba o sync inteiro."""
        respx_mock.get(f"{API_URL}/cliente_contrato_comodato").mock(
            return_value=Response(
                200,
                json={
                    "page": "1",
                    "total": "2",
                    "registros": [
                        {"id_cliente_contrato": "100"},  # falta id → pulado
                        _sample_ixc_comodato(id="2"),
                    ],
                },
            )
        )
        source = IxcEquipmentSource(base_url=BASE_URL, user_id="1", api_token="t")
        dtos = list(source.list_equipment())
        assert len(dtos) == 1
        assert dtos[0].external_id == "2"


# =============================================================================
# Leads — schema crm_canditados + _to_dto
# =============================================================================
from apps.integrations.ixc.leads import IxcLeadSource
from apps.integrations.ixc.opportunities import IxcOpportunitySource
from apps.integrations.ixc.schemas import IxcLeadSchema, IxcOpportunitySchema
from apps.sales.domain.dto import LeadDTO, OpportunityDTO


def _sample_ixc_lead(**overrides: Any) -> dict[str, Any]:
    """Fixture inline de um registro `crm_canditados` do IXC."""
    base = {
        "id": "500",
        "nome": "Prospect Um",
        "telefone": "11999990001",
        "email": "p1@example.test",
        "status_prospeccao": "N",
        "origem": "Indicação",
        "id_vendedor": "7",
        "data_cadastro": "2025-04-05 10:00:00",
        # Campo extra desconhecido — deve ir pra raw_extras
        "id_filial": "1",
    }
    base.update(overrides)
    return base


class TestIxcLeadSchema:
    def test_parses_canonical_response(self) -> None:
        schema = IxcLeadSchema.model_validate(_sample_ixc_lead())
        assert schema.id == "500"
        assert schema.nome == "Prospect Um"
        assert schema.telefone == "11999990001"
        assert schema.status_prospeccao == "N"
        assert schema.origem == "Indicação"
        assert schema.id_vendedor == "7"

    def test_coerces_int_id_to_string(self) -> None:
        schema = IxcLeadSchema.model_validate(_sample_ixc_lead(id=500, id_vendedor=7))
        assert schema.id == "500"
        assert schema.id_vendedor == "7"

    def test_parses_data_cadastro_tz_aware(self) -> None:
        schema = IxcLeadSchema.model_validate(_sample_ixc_lead())
        assert schema.data_cadastro is not None
        assert schema.data_cadastro.tzinfo is not None

    def test_zero_date_becomes_none(self) -> None:
        schema = IxcLeadSchema.model_validate(
            _sample_ixc_lead(data_cadastro="0000-00-00 00:00:00")
        )
        assert schema.data_cadastro is None

    def test_defaults_when_optional_missing(self) -> None:
        schema = IxcLeadSchema.model_validate({"id": "500"})
        assert schema.nome == ""
        assert schema.status_prospeccao == ""
        assert schema.origem == ""
        assert schema.data_cadastro is None

    def test_extras_captured(self) -> None:
        schema = IxcLeadSchema.model_validate(_sample_ixc_lead())
        assert schema.get_extras().get("id_filial") == "1"

    def test_rejects_missing_id(self) -> None:
        raw = _sample_ixc_lead()
        del raw["id"]
        with pytest.raises(Exception):  # noqa: B017 — ValidationError
            IxcLeadSchema.model_validate(raw)


class TestIxcLeadSourceDeclaration:
    def test_implements_port_contract(self) -> None:
        assert IxcLeadSource.source_type == SourceType.IXC
        assert IxcLeadSource.capabilities == frozenset({Capability.LEADS})


class TestIxcLeadToDto:
    def test_basic_mapping(self) -> None:
        schema = IxcLeadSchema.model_validate(_sample_ixc_lead())
        dto = IxcLeadSource._to_dto(schema)
        assert isinstance(dto, LeadDTO)
        assert dto.external_id == "500"
        assert dto.name == "Prospect Um"
        assert dto.phone == "11999990001"
        assert dto.origin == "Indicação"
        assert dto.salesperson_id == "7"

    @pytest.mark.parametrize(
        ("ixc_status", "expected"),
        [
            ("N", "NEW"),
            ("NOVO", "NEW"),
            ("C", "CONTACTED"),
            ("G", "CONVERTED"),
            ("P", "LOST"),
            ("ZZ", "UNKNOWN"),
            ("", "UNKNOWN"),
        ],
    )
    def test_status_mapping(self, ixc_status: str, expected: str) -> None:
        schema = IxcLeadSchema.model_validate(
            _sample_ixc_lead(status_prospeccao=ixc_status)
        )
        dto = IxcLeadSource._to_dto(schema)
        assert dto.status == expected

    def test_lists_single_page_translates_to_dtos(
        self, respx_mock: respx.MockRouter
    ) -> None:
        respx_mock.get(f"{API_URL}/crm_canditados").mock(
            return_value=Response(
                200,
                json={
                    "page": "1",
                    "total": "2",
                    "registros": [
                        _sample_ixc_lead(id="1", status_prospeccao="N"),
                        _sample_ixc_lead(id="2", status_prospeccao="G"),
                    ],
                },
            )
        )
        source = IxcLeadSource(base_url=BASE_URL, user_id="1", api_token="t")
        dtos = list(source.list_leads())
        assert len(dtos) == 2
        assert dtos[0].status == "NEW"
        assert dtos[1].status == "CONVERTED"

    def test_invalid_record_skipped_not_raised(
        self, respx_mock: respx.MockRouter
    ) -> None:
        respx_mock.get(f"{API_URL}/crm_canditados").mock(
            return_value=Response(
                200,
                json={
                    "page": "1",
                    "total": "2",
                    "registros": [
                        {"nome": "Sem id"},  # falta id → pulado
                        _sample_ixc_lead(id="2"),
                    ],
                },
            )
        )
        source = IxcLeadSource(base_url=BASE_URL, user_id="1", api_token="t")
        dtos = list(source.list_leads())
        assert len(dtos) == 1
        assert dtos[0].external_id == "2"


# =============================================================================
# Opportunities — schema crm_negociacoes + _to_dto
# =============================================================================
def _sample_ixc_opportunity(**overrides: Any) -> dict[str, Any]:
    """Fixture inline de um registro `crm_negociacoes` do IXC."""
    base = {
        "id": "800",
        "id_candidato": "500",
        "valor": "1200.00",
        "status": "A",
        "motivo_perda": "",
        "data_criacao": "2025-04-06 09:00:00",
        "id_filial": "1",
    }
    base.update(overrides)
    return base


class TestIxcOpportunitySchema:
    def test_parses_canonical_response(self) -> None:
        schema = IxcOpportunitySchema.model_validate(_sample_ixc_opportunity())
        assert schema.id == "800"
        assert schema.id_candidato == "500"
        assert schema.valor == "1200.00"
        assert schema.status == "A"

    def test_coerces_int_id_to_string(self) -> None:
        schema = IxcOpportunitySchema.model_validate(
            _sample_ixc_opportunity(id=800, id_candidato=500)
        )
        assert schema.id == "800"
        assert schema.id_candidato == "500"

    def test_coerces_comma_decimal(self) -> None:
        schema = IxcOpportunitySchema.model_validate(
            _sample_ixc_opportunity(valor="1200,50")
        )
        assert schema.valor == "1200.50"

    def test_empty_valor_becomes_zero(self) -> None:
        schema = IxcOpportunitySchema.model_validate(
            _sample_ixc_opportunity(valor="")
        )
        assert schema.valor == "0"

    def test_defaults_when_optional_missing(self) -> None:
        schema = IxcOpportunitySchema.model_validate({"id": "800"})
        assert schema.id_candidato == ""
        assert schema.valor == "0"
        assert schema.status == ""
        assert schema.data_criacao is None

    def test_extras_captured(self) -> None:
        schema = IxcOpportunitySchema.model_validate(_sample_ixc_opportunity())
        assert schema.get_extras().get("id_filial") == "1"

    def test_rejects_missing_id(self) -> None:
        raw = _sample_ixc_opportunity()
        del raw["id"]
        with pytest.raises(Exception):  # noqa: B017 — ValidationError
            IxcOpportunitySchema.model_validate(raw)


class TestIxcOpportunitySourceDeclaration:
    def test_implements_port_contract(self) -> None:
        assert IxcOpportunitySource.source_type == SourceType.IXC
        assert IxcOpportunitySource.capabilities == frozenset(
            {Capability.OPPORTUNITIES}
        )


class TestIxcOpportunityToDto:
    def test_basic_mapping(self) -> None:
        schema = IxcOpportunitySchema.model_validate(_sample_ixc_opportunity())
        dto = IxcOpportunitySource._to_dto(schema)
        assert isinstance(dto, OpportunityDTO)
        assert dto.external_id == "800"
        assert dto.lead_external_id == "500"
        assert dto.value == Decimal("1200.00")

    @pytest.mark.parametrize(
        ("ixc_status", "expected"),
        [
            ("A", "OPEN"),
            ("ABERTO", "OPEN"),
            ("G", "WON"),
            ("GANHA", "WON"),
            ("P", "LOST"),
            ("PERDIDA", "LOST"),
            ("ZZ", "UNKNOWN"),
            ("", "UNKNOWN"),
        ],
    )
    def test_status_mapping(self, ixc_status: str, expected: str) -> None:
        schema = IxcOpportunitySchema.model_validate(
            _sample_ixc_opportunity(status=ixc_status)
        )
        dto = IxcOpportunitySource._to_dto(schema)
        assert dto.status == expected

    def test_lists_single_page_translates_to_dtos(
        self, respx_mock: respx.MockRouter
    ) -> None:
        respx_mock.get(f"{API_URL}/crm_negociacoes").mock(
            return_value=Response(
                200,
                json={
                    "page": "1",
                    "total": "2",
                    "registros": [
                        _sample_ixc_opportunity(id="1", status="A"),
                        _sample_ixc_opportunity(id="2", status="G"),
                    ],
                },
            )
        )
        source = IxcOpportunitySource(base_url=BASE_URL, user_id="1", api_token="t")
        dtos = list(source.list_opportunities())
        assert len(dtos) == 2
        assert dtos[0].status == "OPEN"
        assert dtos[1].status == "WON"

    def test_invalid_record_skipped_not_raised(
        self, respx_mock: respx.MockRouter
    ) -> None:
        respx_mock.get(f"{API_URL}/crm_negociacoes").mock(
            return_value=Response(
                200,
                json={
                    "page": "1",
                    "total": "2",
                    "registros": [
                        {"valor": "100.00"},  # falta id → pulado
                        _sample_ixc_opportunity(id="2"),
                    ],
                },
            )
        )
        source = IxcOpportunitySource(base_url=BASE_URL, user_id="1", api_token="t")
        dtos = list(source.list_opportunities())
        assert len(dtos) == 1
        assert dtos[0].external_id == "2"


# =============================================================================
# Bandwidth — schema radusuarios_consumo + _to_dto
# =============================================================================
from datetime import date as _date

from apps.integrations.ixc.bandwidth import IxcBandwidthUsageSource
from apps.integrations.ixc.schemas import IxcBandwidthSchema
from apps.network.domain.dto import BandwidthUsageDTO


def _sample_ixc_consumo(**overrides: Any) -> dict[str, Any]:
    """Fixture inline de um registro `radusuarios_consumo` do IXC."""
    base = {
        "id": "900",
        "id_cliente": "42",
        "acctinputoctets": "5368709120",
        "acctoutputoctets": "1073741824",
        "acctsessiontime": "86400",
        "data": "2025-05-20",
        # Campo extra desconhecido — deve ir pra raw_extras
        "id_filial": "1",
    }
    base.update(overrides)
    return base


class TestIxcBandwidthSchema:
    def test_parses_canonical_response(self) -> None:
        schema = IxcBandwidthSchema.model_validate(_sample_ixc_consumo())
        assert schema.id == "900"
        assert schema.id_cliente == "42"
        assert schema.acctinputoctets == 5368709120
        assert schema.acctoutputoctets == 1073741824
        assert schema.acctsessiontime == 86400
        assert schema.data == _date(2025, 5, 20)

    def test_coerces_int_id_to_string(self) -> None:
        schema = IxcBandwidthSchema.model_validate(
            _sample_ixc_consumo(id=900, id_cliente=42)
        )
        assert schema.id == "900"
        assert schema.id_cliente == "42"

    def test_coerces_numeric_octets(self) -> None:
        schema = IxcBandwidthSchema.model_validate(
            _sample_ixc_consumo(acctinputoctets=1024, acctoutputoctets=2048)
        )
        assert schema.acctinputoctets == 1024
        assert schema.acctoutputoctets == 2048

    def test_empty_octets_become_zero(self) -> None:
        schema = IxcBandwidthSchema.model_validate(
            _sample_ixc_consumo(acctinputoctets="", acctoutputoctets="")
        )
        assert schema.acctinputoctets == 0
        assert schema.acctoutputoctets == 0

    def test_parses_datetime_data_to_date(self) -> None:
        schema = IxcBandwidthSchema.model_validate(
            _sample_ixc_consumo(data="2025-05-20 13:45:00")
        )
        assert schema.data == _date(2025, 5, 20)

    def test_zero_date_becomes_none(self) -> None:
        schema = IxcBandwidthSchema.model_validate(
            _sample_ixc_consumo(data="0000-00-00")
        )
        assert schema.data is None

    def test_defaults_when_optional_missing(self) -> None:
        schema = IxcBandwidthSchema.model_validate({"id": "900"})
        assert schema.id_cliente == ""
        assert schema.acctinputoctets == 0
        assert schema.acctsessiontime == 0
        assert schema.data is None

    def test_extras_captured(self) -> None:
        schema = IxcBandwidthSchema.model_validate(_sample_ixc_consumo())
        assert schema.get_extras().get("id_filial") == "1"

    def test_rejects_missing_id(self) -> None:
        raw = _sample_ixc_consumo()
        del raw["id"]
        with pytest.raises(Exception):  # noqa: B017 — ValidationError
            IxcBandwidthSchema.model_validate(raw)


class TestIxcBandwidthSourceDeclaration:
    def test_implements_port_contract(self) -> None:
        assert IxcBandwidthUsageSource.source_type == SourceType.IXC
        assert IxcBandwidthUsageSource.capabilities == frozenset(
            {Capability.BANDWIDTH}
        )


class TestIxcBandwidthToDto:
    def test_basic_mapping(self) -> None:
        schema = IxcBandwidthSchema.model_validate(_sample_ixc_consumo())
        dto = IxcBandwidthUsageSource._to_dto(schema)
        assert isinstance(dto, BandwidthUsageDTO)
        assert dto.external_id == "900"
        assert dto.customer_external_id == "42"
        assert dto.download_bytes == 5368709120
        assert dto.upload_bytes == 1073741824
        assert dto.session_time == 86400
        assert dto.reference_date == _date(2025, 5, 20)

    def test_lists_single_page_translates_to_dtos(
        self, respx_mock: respx.MockRouter
    ) -> None:
        respx_mock.get(f"{API_URL}/radusuarios_consumo").mock(
            return_value=Response(
                200,
                json={
                    "page": "1",
                    "total": "2",
                    "registros": [
                        _sample_ixc_consumo(id="1"),
                        _sample_ixc_consumo(id="2", acctinputoctets="2048"),
                    ],
                },
            )
        )
        source = IxcBandwidthUsageSource(base_url=BASE_URL, user_id="1", api_token="t")
        dtos = list(source.list_bandwidth_usage())
        assert len(dtos) == 2
        assert all(isinstance(d, BandwidthUsageDTO) for d in dtos)
        assert dtos[1].download_bytes == 2048

    def test_invalid_record_skipped_not_raised(
        self, respx_mock: respx.MockRouter
    ) -> None:
        respx_mock.get(f"{API_URL}/radusuarios_consumo").mock(
            return_value=Response(
                200,
                json={
                    "page": "1",
                    "total": "2",
                    "registros": [
                        {"id_cliente": "42"},  # falta id → pulado
                        _sample_ixc_consumo(id="2"),
                    ],
                },
            )
        )
        source = IxcBandwidthUsageSource(base_url=BASE_URL, user_id="1", api_token="t")
        dtos = list(source.list_bandwidth_usage())
        assert len(dtos) == 1
        assert dtos[0].external_id == "2"
