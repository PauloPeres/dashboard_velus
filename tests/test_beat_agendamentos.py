"""A tela de sync mostra quando cada agendamento rodou pela última vez.

Existe por um caso concreto: o sync do Opa! ficou **oito dias sem rodar** e
ninguém viu. O estado do agendador vivia num arquivo dentro do container do
beat, que sumia a cada deploy — então não havia onde perguntar "quando isto
rodou pela última vez?". Só apareceu porque um painel novo mostrou um número
estranho (19/09/2026).

Com o agendador no banco, `last_run_at` sobrevive ao rollout, e estes testes
travam o que a tela faz com ele: tarefa parada há mais de um dia aparece
marcada, e "nunca rodou" é diferente de "rodou faz tempo".
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any

import pytest
from django.utils import timezone

from apps.sync.views import _agendamentos


@pytest.mark.django_db
class TestAgendamentos:
    def _tarefa(self, nome: str, *, ultima: Any, enabled: bool = True) -> Any:
        from django_celery_beat.models import IntervalSchedule, PeriodicTask

        agenda, _ = IntervalSchedule.objects.get_or_create(
            every=1, period=IntervalSchedule.DAYS
        )
        tarefa = PeriodicTask.objects.create(
            name=nome, task=f"apps.fake.{nome}", interval=agenda, enabled=enabled
        )
        if ultima is not None:
            PeriodicTask.objects.filter(pk=tarefa.pk).update(
                last_run_at=ultima, total_run_count=3
            )
        return tarefa

    def test_tarefa_recente_nao_e_marcada(self) -> None:
        self._tarefa("sync-diario", ultima=timezone.now() - timedelta(hours=2))
        linha = _agendamentos(timezone.now())[0]
        assert linha["atrasada"] is False
        assert linha["nunca_rodou"] is False

    def test_tarefa_parada_ha_dias_e_marcada(self) -> None:
        """O caso do Opa!: rodava diariamente e parou, sem deixar rastro."""
        self._tarefa("sync-opa-atendimento-daily", ultima=timezone.now() - timedelta(days=8))
        linha = _agendamentos(timezone.now())[0]
        assert linha["atrasada"] is True
        assert linha["nunca_rodou"] is False

    def test_tarefa_nova_que_ainda_nao_teve_a_vez_nao_e_atraso(self) -> None:
        """No dia em que o agendador mudou, as 19 entradas nasceram sem
        histórico e a tela marcou todas de uma vez. Mural de alarme falso ensina
        a ignorar o alarme — o oposto do que esta lista existe para fazer."""
        self._tarefa("tarefa-nova", ultima=None)
        linha = _agendamentos(timezone.now())[0]
        assert linha["nunca_rodou"] is True
        assert linha["atrasada"] is False

    def test_entrada_antiga_que_nunca_rodou_e_atraso(self) -> None:
        """Aqui sim: a entrada existe há dias e nada aconteceu."""
        from django_celery_beat.models import PeriodicTask

        tarefa = self._tarefa("tarefa-esquecida", ultima=None)
        PeriodicTask.objects.filter(pk=tarefa.pk).update(
            date_changed=timezone.now() - timedelta(days=5)
        )
        linha = _agendamentos(timezone.now())[0]
        assert linha["nunca_rodou"] is True
        assert linha["atrasada"] is True

    def test_desabilitada_fica_de_fora(self) -> None:
        """Tarefa desligada de propósito não é problema a resolver."""
        self._tarefa("desligada", ultima=None, enabled=False)
        assert _agendamentos(timezone.now()) == []
