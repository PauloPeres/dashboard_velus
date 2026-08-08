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
    OpaEtiquetaSchema,
    OpaMensagemSchema,
    OpaMotivoSchema,
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
        # motivo real vem como {idMotivo, idAtendente, idDepartamento, data} — só id
        "motivos": [{"idMotivo": "mot-conexao", "idAtendente": "u9"}],
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
        assert schema.motivo_ids == ["mot-conexao"]

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

    def test_tag_ids_extracted_from_tags_dedup_ordered(self) -> None:
        # tags vem como aplicacoes {_id, data, id_tag, ...}; a identidade e o
        # id_tag. Dedup preservando ordem; ainda fica cru em raw_extras.
        schema = OpaAtendimentoSchema.model_validate(
            _sample_atendimento(
                tags=[
                    {"_id": "app1", "id_tag": "t-suporte", "data": "2026-01-01"},
                    {"_id": "app2", "id_tag": "t-comercial"},
                    {"_id": "app3", "id_tag": "t-suporte"},  # duplicada
                ]
            )
        )
        assert schema.tag_ids == ["t-suporte", "t-comercial"]
        assert schema.get_extras().get("tags")  # preservado no raw

    def test_tag_ids_empty_when_no_tags(self) -> None:
        schema = OpaAtendimentoSchema.model_validate(
            _sample_atendimento(tags=[])
        )
        assert schema.tag_ids == []

    def test_motivo_ids_extracted_dedup_ordered(self) -> None:
        schema = OpaAtendimentoSchema.model_validate(
            _sample_atendimento(
                motivos=[
                    {"idMotivo": "m1", "idAtendente": "u9"},
                    {"idMotivo": "m2"},
                    {"idMotivo": "m1"},  # duplicado
                ]
            )
        )
        assert schema.motivo_ids == ["m1", "m2"]

    def test_motivo_ids_empty_when_no_motivos(self) -> None:
        schema = OpaAtendimentoSchema.model_validate(
            _sample_atendimento(motivos=[])
        )
        assert schema.motivo_ids == []


class TestOpaEtiquetaSchema:
    def test_parses_catalog_record(self) -> None:
        schema = OpaEtiquetaSchema.model_validate(
            {"_id": "t-suporte", "nome": "Suporte", "cor": "blue"}
        )
        assert schema.id == "t-suporte"
        assert schema.nome == "Suporte"
        assert schema.cor == "blue"


class TestOpaMotivoSchema:
    def test_parses_catalog_record_nome_from_motivo_field(self) -> None:
        # O catalogo usa a chave "motivo" pro nome (nao "nome").
        schema = OpaMotivoSchema.model_validate(
            {"_id": "64c2a7ed", "motivo": "comercial", "departamentos": ["d1"]}
        )
        assert schema.id == "64c2a7ed"
        assert schema.nome == "comercial"


class TestOpaMensagemSchema:
    def test_direction_from_tipo_destinatario(self) -> None:
        # tipoDestinatario e o DESTINATARIO (quem recebe): destinatario
        # `usuarios` (atendente) => mensagem do cliente; `clientes_users`
        # (cliente) => mensagem do atendente.
        from_client = OpaMensagemSchema.model_validate(
            {"_id": "m1", "id_rota": "a1", "mensagem": "oi", "tipo": "texto",
             "tipoDestinatario": "usuarios", "data": "2023-01-10T12:01:00.000Z"}
        )
        from_agent = OpaMensagemSchema.model_validate(
            {"_id": "m2", "id_rota": "a1", "tipoDestinatario": "clientes_users"}
        )
        system = OpaMensagemSchema.model_validate({"_id": "m3", "id_rota": "a1"})
        assert from_client.direction == "CLIENT"
        assert from_agent.direction == "AGENT"
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
        # motivo_ids crus no adapter; nomes só resolvem no run_opa_sync
        assert dtos[0].motivo_ids == ["mot-conexao"]
        assert dtos[0].motivos == []

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

    def test_list_etiquetas(self, respx_mock: respx.MockRouter) -> None:
        respx_mock.get(f"{API_URL}/etiqueta/").mock(
            return_value=Response(
                200,
                json={"data": [
                    {"_id": "t-suporte", "nome": "Suporte", "cor": "blue"},
                    {"_id": "t-comercial", "nome": "Comercial", "cor": "green"},
                ]},
            )
        )

        source = OpaAtendimentoSource(base_url=BASE_URL, token=TOKEN)
        etiquetas = list(source.list_etiquetas())

        assert [e.external_id for e in etiquetas] == ["t-suporte", "t-comercial"]
        assert etiquetas[0].nome == "Suporte"
        assert etiquetas[0].cor == "blue"

    def test_list_motivos(self, respx_mock: respx.MockRouter) -> None:
        respx_mock.get(f"{API_URL}/atendimento/motivo").mock(
            return_value=Response(
                200,
                json={"data": [
                    {"_id": "m1", "motivo": "comercial", "departamentos": ["d1"]},
                    {"_id": "m2", "motivo": "Suporte", "departamentos": ["d2"]},
                ]},
            )
        )

        source = OpaAtendimentoSource(base_url=BASE_URL, token=TOKEN)
        motivos = list(source.list_motivos())

        assert [m.external_id for m in motivos] == ["m1", "m2"]
        assert motivos[0].nome == "comercial"

    def test_list_atendimentos_carries_tag_and_motivo_ids(
        self, respx_mock: respx.MockRouter
    ) -> None:
        respx_mock.get(f"{API_URL}/atendimento").mock(
            return_value=Response(
                200,
                json={"data": [
                    _sample_atendimento(
                        _id="a1",
                        motivos=[{"idMotivo": "mot-1"}],
                        tags=[
                            {"_id": "app1", "id_tag": "t-suporte"},
                            {"_id": "app2", "id_tag": "t-comercial"},
                        ],
                    ),
                ]},
            )
        )
        source = OpaAtendimentoSource(base_url=BASE_URL, token=TOKEN)
        dtos = list(source.list_atendimentos())
        assert dtos[0].tag_ids == ["t-suporte", "t-comercial"]
        assert dtos[0].motivo_ids == ["mot-1"]
        # nomes so sao resolvidos no run_opa_sync (via catalogo), aqui ainda vazio
        assert dtos[0].tags == []
        assert dtos[0].motivos == []

    def test_list_atendentes(self, respx_mock: respx.MockRouter) -> None:
        respx_mock.get(f"{API_URL}/usuario/").mock(
            return_value=Response(
                200,
                json={"data": [
                    {"_id": "u9", "nome": "Felipe", "status": "A", "tipo": "atendente"},
                ]},
            )
        )

        source = OpaAtendimentoSource(base_url=BASE_URL, token=TOKEN)
        atendentes = list(source.list_atendentes())

        assert atendentes[0].external_id == "u9"
        assert atendentes[0].nome == "Felipe"

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
        # m1 destinatario=clientes_users => enviada pelo atendente; m2
        # destinatario=usuarios => enviada pelo cliente.
        assert msgs[0].direction == "AGENT"
        assert msgs[1].direction == "CLIENT"
