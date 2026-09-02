"""Testes da listagem global de mensagens do Opa! — a base do backfill de volume.

O que importa provar aqui não é o mapeamento (já coberto em
`test_opa_adapter`), e sim o mecanismo que torna o backfill viável:

- paginação por `skip` a partir de um offset arbitrário, com o offset ABSOLUTO
  devolvido junto (é ele que vira checkpoint);
- busca binária da data — sem ela, alcançar 2026 custaria 28 mil chamadas;
- os campos novos que a página de custo lê (canal e janela de 24h), incluindo a
  diferença entre "não informado" e "dentro da janela".
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

import respx
from httpx import Response

from apps.integrations.opa.atendimento import OpaAtendimentoSource

BASE_URL = "https://opa.test.net.br"
API_URL = f"{BASE_URL}/api/v1"
TOKEN = "jwt-token-abc"


def _msg(
    _id: str,
    *,
    data: str = "2026-08-01T12:00:00.000Z",
    destinatario: str = "clientes_users",
    tipo: str = "texto",
    canal: str = "canal-0800",
    fora: bool | None = None,
) -> dict[str, Any]:
    raw: dict[str, Any] = {
        "_id": _id,
        "id_rota": f"rota-{_id}",
        "mensagem": "oi",
        "tipo": tipo,
        "tipoDestinatario": destinatario,
        "canalComunicacao": canal,
        "data": data,
    }
    if fora is not None:
        raw["envioForaJanela24h"] = fora
    return raw


class TestListMensagensGlobal:
    def test_paginates_from_start_skip_yielding_absolute_offsets(
        self, respx_mock: respx.MockRouter
    ) -> None:
        paginas = [
            {"data": [_msg("m1"), _msg("m2")]},
            {"data": [_msg("m3")]},  # menor que o limit -> última
        ]
        skips: list[int] = []

        def handler(request: Any) -> Response:
            body = json.loads(request.content)
            skips.append(body["options"]["skip"])
            return Response(200, json=paginas[len(skips) - 1])

        respx_mock.get(f"{API_URL}/atendimento/mensagem").mock(side_effect=handler)

        source = OpaAtendimentoSource(base_url=BASE_URL, token=TOKEN)
        resultado = list(
            source.list_mensagens_global(start_skip=1000, page_size=2)
        )

        assert skips == [1000, 1002]
        # O offset é absoluto: é o que o checkpoint guarda pra retomar.
        assert [offset for offset, _ in resultado] == [1000, 1001, 1002]
        assert [dto.external_id for _, dto in resultado] == ["m1", "m2", "m3"]

    def test_stops_at_max_pages(self, respx_mock: respx.MockRouter) -> None:
        respx_mock.get(f"{API_URL}/atendimento/mensagem").mock(
            return_value=Response(200, json={"data": [_msg("m1"), _msg("m2")]})
        )
        source = OpaAtendimentoSource(base_url=BASE_URL, token=TOKEN)
        resultado = list(
            source.list_mensagens_global(page_size=2, max_pages=2)
        )
        assert len(resultado) == 4

    def test_maps_canal_and_janela_fields(self, respx_mock: respx.MockRouter) -> None:
        respx_mock.get(f"{API_URL}/atendimento/mensagem").mock(
            return_value=Response(
                200,
                json={
                    "data": [
                        _msg("m1", canal="canal-comercial", fora=True),
                        _msg("m2", destinatario="usuarios"),  # recebida: sem flag
                    ]
                },
            )
        )
        source = OpaAtendimentoSource(base_url=BASE_URL, token=TOKEN)
        dtos = [dto for _, dto in source.list_mensagens_global(page_size=2, max_pages=1)]

        assert dtos[0].canal_external_id == "canal-comercial"
        assert dtos[0].fora_janela_24h is True
        assert dtos[0].direction == "AGENT"
        # "não informado" (None) não pode virar False: um vira "sem dado", o
        # outro vira "dentro da janela" — e só o segundo entra na conta de custo.
        assert dtos[1].fora_janela_24h is None
        assert dtos[1].direction == "CLIENT"

    def test_does_not_store_message_text(self, respx_mock: respx.MockRouter) -> None:
        """Backfill de volume é metadado: o texto não pode vazar pro DTO."""
        respx_mock.get(f"{API_URL}/atendimento/mensagem").mock(
            return_value=Response(200, json={"data": [_msg("m1")]})
        )
        source = OpaAtendimentoSource(base_url=BASE_URL, token=TOKEN)
        dtos = [dto for _, dto in source.list_mensagens_global(page_size=1, max_pages=1)]
        assert dtos[0].texto == ""


class TestFindSkipForDate:
    def test_binary_searches_first_offset_at_or_after_target(
        self, respx_mock: respx.MockRouter
    ) -> None:
        # Coleção sintética: 1 mensagem por dia a partir de 01/01/2026.
        def data_do_offset(offset: int) -> str:
            return f"2026-01-{offset + 1:02d}T00:00:00.000Z"

        chamadas: list[int] = []

        def handler(request: Any) -> Response:
            body = json.loads(request.content)
            skip = body["options"]["skip"]
            chamadas.append(skip)
            if skip >= 20:  # fim da coleção
                return Response(200, json={"data": []})
            return Response(200, json={"data": [_msg("m", data=data_do_offset(skip))]})

        respx_mock.get(f"{API_URL}/atendimento/mensagem").mock(side_effect=handler)

        source = OpaAtendimentoSource(base_url=BASE_URL, token=TOKEN)
        offset = source.find_skip_for_date(
            datetime(2026, 1, 11, tzinfo=UTC), ceiling=32
        )

        # 11/01 é o registro de offset 10.
        assert offset == 10
        # Bissecção, não varredura: em 32 registros são ~5 chamadas, não 10.
        assert len(chamadas) <= 8
