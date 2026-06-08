"""Testes do adapter Opa! Suite.

Usa `respx` para mockar HTTPX sem rede real. Cobre:
- Anti-Corruption Layer (Pydantic) — id_cliente/setor polimorficos, datas ISO
- Mapeamento campo-a-campo Opa! -> AtendimentoDTO/MensagemDTO
- Status F/EA/A -> CLOSED/IN_PROGRESS/OPEN
- Paginacao via `options.skip` (NAO page/offset)
- Header Authorization: Bearer
- rating likert extraido so do GET populado
"""

from __future__ import annotations

import json
from typing import Any

import respx
from httpx import Response

from apps.atendimento.domain.dto import AtendimentoDTO, MensagemDTO
from apps.integrations.opa.atendimento import OpaAtendimentoSource
from apps.integrations.opa.client import OpaHttpClient
from apps.integrations.opa.schemas import (
    OpaAtendimentoSchema,
    OpaMensagemSchema,
)
from apps.integrations.shared.enums import Capability, SourceType

BASE_URL = "https://opa.test.net.br"
API_URL = f"{BASE_URL}/api/v1"
TOKEN = "jwt-token-abc"


def _sample_atendimento(**overrides: Any) -> dict[str, Any]:
    base = {
        "_id": "a1",
        "id_cliente": "cli-opaco-1",  # na listagem vem id opaco
        "id_atendente": "u9",
        "setor": "dep-suporte",
        "status": "F",
        "canal": "whatsapp",
        "protocolo": "OPA202301",
        "motivos": [{"nome": "Sem conexão"}],
        "evaluations": [],
        "date": "2023-01-10T12:00:00.000Z",
        "fim": "2023-01-10T13:30:00.000Z",
        "origem": "bot",  # campo extra -> raw_extras
    }
    base.update(overrides)
    return base


# =============================================================================
# Schema Pydantic — Anti-Corruption Layer
# =============================================================================
class TestOpaAtendimentoSchema:
    def test_parses_list_record_with_opaque_client_id(self) -> None:
        schema = OpaAtendimentoSchema.model_validate(_sample_atendimento())
        assert schema.id == "a1"
        assert schema.customer_external_id == "cli-opaco-1"
        assert schema.customer_document == ""  # so vem no GET populado
        assert schema.departamento_external_id == "dep-suporte"
        assert schema.date is not None and schema.date.tzinfo is not None
        assert schema.motivos_names == ["Sem conexão"]

    def test_parses_populated_client_object(self) -> None:
        schema = OpaAtendimentoSchema.model_validate(
            _sample_atendimento(
                id_cliente={
                    "_id": "cli-opaco-1",
                    "nome": "Bruna",
                    "cpf_cnpj": "123.456.789-01",
                }
            )
        )
        assert schema.customer_external_id == "cli-opaco-1"
        assert schema.customer_document == "123.456.789-01"
        assert schema.customer_name == "Bruna"

    def test_rating_extracted_only_from_evaluations(self) -> None:
        schema = OpaAtendimentoSchema.model_validate(
            _sample_atendimento(
                evaluations=[
                    {"metric": "likert", "likert": {"rating": 4, "topic": "all"}}
                ]
            )
        )
        assert schema.rating == 4

    def test_rating_none_when_no_evaluations(self) -> None:
        schema = OpaAtendimentoSchema.model_validate(_sample_atendimento())
        assert schema.rating is None

    def test_coerces_int_id_to_string(self) -> None:
        schema = OpaAtendimentoSchema.model_validate(_sample_atendimento(_id=123))
        assert schema.id == "123"

    def test_extras_captured(self) -> None:
        schema = OpaAtendimentoSchema.model_validate(_sample_atendimento())
        assert schema.get_extras().get("origem") == "bot"


class TestOpaMensagemSchema:
    def test_direction_from_tipo_destinatario(self) -> None:
        agent = OpaMensagemSchema.model_validate(
            {"_id": "m1", "id_rota": "a1", "mensagem": "oi", "tipo": "texto",
             "tipoDestinatario": "usuarios", "data": "2023-01-10T12:01:00.000Z"}
        )
        client = OpaMensagemSchema.model_validate(
            {"_id": "m2", "id_rota": "a1", "tipoDestinatario": "clientes_users"}
        )
        system = OpaMensagemSchema.model_validate({"_id": "m3", "id_rota": "a1"})
        assert agent.direction == "AGENT"
        assert client.direction == "CLIENT"
        assert system.direction == "SYSTEM"


# =============================================================================
# Source — declaração do port
# =============================================================================
class TestOpaSourceDeclaration:
    def test_implements_port_contract(self) -> None:
        assert OpaAtendimentoSource.source_type == SourceType.OPA
        assert OpaAtendimentoSource.capabilities == frozenset(
            {Capability.ATENDIMENTO}
        )


# =============================================================================
# Mock HTTP — list_atendimentos ponta a ponta
# =============================================================================
class TestOpaListAtendimentos:
    def test_list_translates_to_dtos_and_maps_status(
        self, respx_mock: respx.MockRouter
    ) -> None:
        respx_mock.get(f"{API_URL}/atendimento").mock(
            return_value=Response(
                200,
                json={
                    "status": "success",
                    "data": [
                        _sample_atendimento(_id="a1", status="F"),
                        _sample_atendimento(_id="a2", status="EA"),
                        _sample_atendimento(_id="a3", status="A"),
                    ],
                },
            )
        )

        source = OpaAtendimentoSource(base_url=BASE_URL, token=TOKEN)
        dtos = list(source.list_atendimentos())

        assert len(dtos) == 3
        assert all(isinstance(d, AtendimentoDTO) for d in dtos)
        assert dtos[0].status == "CLOSED"
        assert dtos[1].status == "IN_PROGRESS"
        assert dtos[2].status == "OPEN"
        assert dtos[0].canal == "whatsapp"
        assert dtos[0].motivos == ["Sem conexão"]

    def test_sends_bearer_token(self, respx_mock: respx.MockRouter) -> None:
        route = respx_mock.get(f"{API_URL}/atendimento").mock(
            return_value=Response(200, json={"data": []})
        )
        source = OpaAtendimentoSource(base_url=BASE_URL, token=TOKEN)
        list(source.list_atendimentos())
        assert route.calls.last.request.headers["Authorization"] == f"Bearer {TOKEN}"

    def test_paginates_via_skip(self, respx_mock: respx.MockRouter) -> None:
        """Opa! pagina por options.skip — verifica skip crescente e parada."""
        pages = [
            {"data": [_sample_atendimento(_id="a1"), _sample_atendimento(_id="a2")]},
            {"data": [_sample_atendimento(_id="a3"), _sample_atendimento(_id="a4")]},
            {"data": [_sample_atendimento(_id="a5")]},  # última (menor que limit)
        ]
        seen_skips: list[int] = []

        def handler(request: Any) -> Response:
            body = json.loads(request.content)
            seen_skips.append(body["options"]["skip"])
            return Response(200, json=pages[len(seen_skips) - 1])

        respx_mock.get(f"{API_URL}/atendimento").mock(side_effect=handler)

        with OpaHttpClient(base_url=BASE_URL, token=TOKEN) as client:
            items = list(client.paginate_opa("atendimento", page_size=2))

        assert seen_skips == [0, 2, 4]
        assert len(items) == 5

    def test_get_atendimento_populated_extracts_rating(
        self, respx_mock: respx.MockRouter
    ) -> None:
        respx_mock.get(f"{API_URL}/atendimento/a1").mock(
            return_value=Response(
                200,
                json={
                    "data": _sample_atendimento(
                        _id="a1",
                        id_cliente={
                            "_id": "cli-opaco-1",
                            "nome": "Bruna",
                            "cpf_cnpj": "123.456.789-01",
                        },
                        evaluations=[
                            {"metric": "likert", "likert": {"rating": 5}}
                        ],
                    )
                },
            )
        )

        source = OpaAtendimentoSource(base_url=BASE_URL, token=TOKEN)
        dto = source.get_atendimento("a1")

        assert dto is not None
        assert dto.rating == 5
        assert dto.customer_document == "12345678901"  # normalizado
        assert dto.customer_name == "Bruna"

    def test_list_departamentos_and_clientes(
        self, respx_mock: respx.MockRouter
    ) -> None:
        respx_mock.get(f"{API_URL}/departamento/").mock(
            return_value=Response(
                200,
                json={"data": [{"_id": "dep-suporte", "nome": "Suporte", "status": "A"}]},
            )
        )
        respx_mock.get(f"{API_URL}/cliente/").mock(
            return_value=Response(
                200,
                json={"data": [
                    {"_id": "cli-opaco-1", "nome": "Bruna", "cpf_cnpj": "123.456.789-01"},
                    {"_id": "cli-sem-doc", "nome": "Sem Doc", "cpf_cnpj": ""},
                ]},
            )
        )

        source = OpaAtendimentoSource(base_url=BASE_URL, token=TOKEN)
        deps = list(source.list_departamentos())
        clientes = list(source.list_clientes())

        assert deps[0].external_id == "dep-suporte"
        assert deps[0].nome == "Suporte"
        assert clientes[0].document == "12345678901"  # normalizado
        assert clientes[1].document == ""

    def test_list_mensagens_maps_direction(
        self, respx_mock: respx.MockRouter
    ) -> None:
        respx_mock.get(f"{API_URL}/atendimento/mensagem").mock(
            return_value=Response(
                200,
                json={"data": [
                    {"_id": "m1", "id_rota": "a1", "mensagem": "oi",
                     "tipo": "texto", "tipoDestinatario": "clientes_users",
                     "data": "2023-01-10T12:01:00.000Z"},
                    {"_id": "m2", "id_rota": "a1", "mensagem": "olá",
                     "tipo": "texto", "tipoDestinatario": "usuarios"},
                ]},
            )
        )

        source = OpaAtendimentoSource(base_url=BASE_URL, token=TOKEN)
        msgs = list(source.list_mensagens("a1"))

        assert len(msgs) == 2
        assert all(isinstance(m, MensagemDTO) for m in msgs)
        assert msgs[0].direction == "CLIENT"
        assert msgs[1].direction == "AGENT"
