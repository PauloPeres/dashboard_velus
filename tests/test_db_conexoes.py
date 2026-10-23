"""Higiene de conexão do worker — `apps.shared.db.descarta_conexoes_quebradas`.

Em 21/10/2026 um sync interrompido no meio de uma query deixou a conexão de
cada processo do worker num estado irrecuperável, e toda task seguinte falhou
por 17 horas ("the connection is closed").
"""

from __future__ import annotations

import pytest
from django.db import connection

from apps.shared.db import descarta_conexoes_quebradas


@pytest.mark.django_db(transaction=True)
class TestDescartaConexoesQuebradas:
    def _consulta(self) -> int:
        with connection.cursor() as cursor:
            cursor.execute("SELECT 1")
            return cursor.fetchone()[0]

    def test_conexao_saudavel_fica(self) -> None:
        self._consulta()
        raw = connection.connection

        assert descarta_conexoes_quebradas() == 0
        assert connection.connection is raw

    def test_conexao_fechada_por_baixo_e_trocada(self) -> None:
        self._consulta()
        connection.connection.close()  # o que sobra de um socket morto

        assert descarta_conexoes_quebradas() == 1
        assert self._consulta() == 1  # o Django abre outra

    def test_sinal_do_celery_limpa_antes_da_task(self) -> None:
        from config.celery import _descarta_conexoes_quebradas

        self._consulta()
        connection.connection.close()

        _descarta_conexoes_quebradas()
        assert self._consulta() == 1
