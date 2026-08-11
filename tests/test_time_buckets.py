"""Eixo do tempo das séries — `apps.analytics.application.time_buckets` (#100).

Módulo Python puro: nenhum teste aqui toca banco. A regra de granularidade é a
mesma da #89 (era privada da triagem); estes testes agora são o contrato dela
pra todas as séries que passaram a receber janela livre.
"""

from __future__ import annotations

from datetime import date, timedelta
from itertools import pairwise

import pytest

from apps.analytics.application import time_buckets as tb


class TestGranularidade:
    @pytest.mark.parametrize(
        ("span_days", "esperado"),
        [
            (1, tb.DAY),      # "Hoje"
            (7, tb.DAY),
            (30, tb.DAY),
            (31, tb.DAY),     # último dia da faixa diária
            (32, tb.WEEK),    # primeiro da semanal
            (90, tb.WEEK),
            (120, tb.WEEK),   # último da faixa semanal
            (121, tb.MONTH),  # primeiro da mensal
            (365, tb.MONTH),
        ],
    )
    def test_faixas(self, span_days: int, esperado: str) -> None:
        start = date(2026, 1, 1)
        end = start + timedelta(days=span_days - 1)
        assert tb.series_granularity(start, end) == esperado

    def test_granularidade_explicita_vence_o_automatico(self) -> None:
        start, end = date(2026, 1, 1), date(2026, 12, 31)
        assert tb.resolve_granularity(start, end) == tb.MONTH
        assert tb.resolve_granularity(start, end, tb.DAY) == tb.DAY

    def test_valor_desconhecido_cai_no_automatico(self) -> None:
        """Querystring torta não pode derrubar a página."""
        start, end = date(2026, 1, 1), date(2026, 1, 7)
        assert tb.resolve_granularity(start, end, "banana") == tb.DAY


class TestBuckets:
    def test_dia_rende_um_bucket_por_dia(self) -> None:
        buckets = tb.build_buckets(date(2026, 3, 2), date(2026, 3, 5))
        assert [b.key for b in buckets] == [
            "2026-03-02", "2026-03-03", "2026-03-04", "2026-03-05",
        ]
        assert buckets[0].label == "02/03"
        assert buckets[0].start == buckets[0].end == date(2026, 3, 2)

    def test_semana_comeca_na_segunda_e_engloba_a_janela(self) -> None:
        # 2026-03-04 é uma quarta; o bucket dela começa na segunda, 02/03.
        buckets = tb.build_buckets(
            date(2026, 3, 4), date(2026, 3, 4), granularity=tb.WEEK
        )
        assert len(buckets) == 1
        assert buckets[0].start == date(2026, 3, 2)
        assert buckets[0].end == date(2026, 3, 8)

    def test_mes_parcial_continua_sendo_um_ponto_so(self) -> None:
        buckets = tb.build_buckets(
            date(2026, 3, 10), date(2026, 3, 20), granularity=tb.MONTH
        )
        assert len(buckets) == 1
        assert buckets[0].key == "2026-03"
        assert buckets[0].start == date(2026, 3, 1)
        assert buckets[0].end == date(2026, 3, 31)

    def test_eixo_nao_tem_buraco(self) -> None:
        """O eixo vem da JANELA, não dos dados: dia vazio é zero, não sumiço."""
        buckets = tb.build_buckets(date(2026, 1, 1), date(2026, 12, 31))
        assert len(buckets) == 12
        assert [b.key for b in buckets][:3] == ["2026-01", "2026-02", "2026-03"]

    def test_virada_de_ano_no_mes(self) -> None:
        buckets = tb.build_buckets(
            date(2025, 11, 15), date(2026, 2, 3), granularity=tb.MONTH
        )
        assert [b.key for b in buckets] == [
            "2025-11", "2025-12", "2026-01", "2026-02",
        ]

    def test_buckets_sao_contiguos_e_sem_sobreposicao(self) -> None:
        for gran in (tb.DAY, tb.WEEK, tb.MONTH):
            buckets = tb.build_buckets(date(2026, 1, 1), date(2026, 6, 30), gran)
            for anterior, seguinte in pairwise(buckets):
                assert anterior.end < seguinte.start
                assert (seguinte.start - anterior.end).days == 1

    def test_janela_de_um_dia(self) -> None:
        buckets = tb.build_buckets(date(2026, 3, 4), date(2026, 3, 4))
        assert len(buckets) == 1
        assert buckets[0].key == "2026-03-04"


class TestCompatibilidadeComATriagem:
    """A #89 não pode mudar de comportamento por causa da extração (#100)."""

    def test_triagem_delega_pro_modulo(self) -> None:
        from datetime import datetime
        from zoneinfo import ZoneInfo

        from apps.analytics.application.aggregations import triagem_trend_granularity

        sp = ZoneInfo("America/Sao_Paulo")
        start = datetime(2026, 1, 1, 0, 0, tzinfo=sp)
        for span, esperado in ((10, tb.DAY), (60, tb.WEEK), (200, tb.MONTH)):
            end = start + timedelta(days=span - 1)
            assert triagem_trend_granularity(start, end) == esperado
