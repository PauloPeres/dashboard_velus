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
