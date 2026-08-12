"""Checkpoint não avança em rodada vazia — issue #132.

O incidente: o incremental de faturas avançava `last_processed_at` pra "agora"
mesmo importando ZERO registros. A rodada seguinte perguntava "o que mudou desde
agora?", recebia nada, e gravava de novo que estava em dia. Uma vez atrasado, o
sync se convencia a cada 3 horas de que estava atualizado — e não havia caminho
de volta. Seis semanas de caixa sumiram assim, sempre com status COMPLETED.
"""

from __future__ import annotations

from datetime import timedelta

import pytest
from django.utils import timezone

from apps.sync.models import SyncCheckpoint, SyncMode
from apps.sync.tasks import EMPTY_RUNS_ALERTA, _atualiza_checkpoint
from apps.tenancy.models import Organization


class _Log:
    """Captura o que o sync teria logado."""

    def __init__(self) -> None:
        self.warnings: list[tuple[str, dict]] = []

    def warning(self, evento: str, **kw: object) -> None:
        self.warnings.append((evento, kw))


@pytest.fixture
def checkpoint(organization_a: Organization) -> SyncCheckpoint:
    return SyncCheckpoint.objects.create(
        organization=organization_a,
        source_type="IXC",
        capability="INVOICES",
        last_processed_at=timezone.now() - timedelta(days=40),
    )


@pytest.mark.django_db
class TestCheckpointVazio:
    def test_rodada_vazia_nao_avanca_o_marcador(
        self, checkpoint: SyncCheckpoint
    ) -> None:
        """O bug da #132 em uma linha."""
        antes = checkpoint.last_processed_at
        _atualiza_checkpoint(checkpoint, 0, SyncMode.INCREMENTAL, _Log())
        checkpoint.refresh_from_db()
        assert checkpoint.last_processed_at == antes

    def test_rodada_com_registro_avanca(self, checkpoint: SyncCheckpoint) -> None:
        antes = checkpoint.last_processed_at
        _atualiza_checkpoint(checkpoint, 42, SyncMode.INCREMENTAL, _Log())
        checkpoint.refresh_from_db()
        assert checkpoint.last_processed_at > antes

    def test_vazias_seguidas_sao_contadas(self, checkpoint: SyncCheckpoint) -> None:
        for _ in range(3):
            _atualiza_checkpoint(checkpoint, 0, SyncMode.INCREMENTAL, _Log())
        checkpoint.refresh_from_db()
        assert checkpoint.consecutive_empty_runs == 3

    def test_um_registro_zera_o_contador(self, checkpoint: SyncCheckpoint) -> None:
        for _ in range(5):
            _atualiza_checkpoint(checkpoint, 0, SyncMode.INCREMENTAL, _Log())
        _atualiza_checkpoint(checkpoint, 1, SyncMode.INCREMENTAL, _Log())
        checkpoint.refresh_from_db()
        assert checkpoint.consecutive_empty_runs == 0

    def test_alerta_a_partir_do_limite(self, checkpoint: SyncCheckpoint) -> None:
        """Uma rodada vazia é rotina; muitas seguidas é o incidente."""
        log = _Log()
        for _ in range(EMPTY_RUNS_ALERTA - 1):
            _atualiza_checkpoint(checkpoint, 0, SyncMode.INCREMENTAL, log)
        assert log.warnings == []

        _atualiza_checkpoint(checkpoint, 0, SyncMode.INCREMENTAL, log)
        assert len(log.warnings) == 1
        evento, kw = log.warnings[0]
        assert evento == "sync_sem_registros_ha_muitas_rodadas"
        assert kw["consecutive_empty_runs"] == EMPTY_RUNS_ALERTA

    def test_bootstrap_nao_mexe_no_checkpoint(
        self, checkpoint: SyncCheckpoint
    ) -> None:
        antes = checkpoint.last_processed_at
        _atualiza_checkpoint(checkpoint, 0, SyncMode.BOOTSTRAP, _Log())
        checkpoint.refresh_from_db()
        assert checkpoint.last_processed_at == antes
        assert checkpoint.consecutive_empty_runs == 0

    def test_janela_se_recupera_sozinha(self, checkpoint: SyncCheckpoint) -> None:
        """A consequência prática de não avançar: o buraco fecha sem intervenção.

        Depois de várias rodadas vazias, o `since` continua sendo o último
        momento em que ENTROU dado — então quando a fonte voltar, a rodada
        seguinte busca tudo o que ficou pra trás.
        """
        marcador = checkpoint.last_processed_at
        for _ in range(10):
            _atualiza_checkpoint(checkpoint, 0, SyncMode.INCREMENTAL, _Log())
        checkpoint.refresh_from_db()
        assert checkpoint.last_processed_at == marcador
        assert timezone.now() - checkpoint.last_processed_at > timedelta(days=39)


@pytest.mark.django_db
class TestRebuildForaDoSync:
    def test_sync_completed_despacha_task_em_vez_de_rodar_inline(
        self, organization_a: Organization, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """O rebuild inline foi o que matou o worker por OOM (#132)."""
        from apps.analytics import signals as analytics_signals
        from apps.analytics import tasks as analytics_tasks

        despachos: list[dict] = []
        monkeypatch.setattr(
            analytics_tasks.rebuild_after_sync,
            "delay",
            lambda **kw: despachos.append(kw),
        )
        monkeypatch.setattr(
            analytics_signals, "compute_churn_risk_scores", lambda org: {}
        )

        analytics_signals._on_sync_completed(
            sender=None,
            organization=organization_a,
            capability="INVOICES",
            records_processed=111_526,
        )
        assert despachos == [
            {"organization_id": organization_a.pk, "capability": "INVOICES"}
        ]

    def test_sync_sem_registros_nao_dispara_rebuild(
        self, organization_a: Organization, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from apps.analytics import tasks as analytics_tasks
        from apps.analytics.signals import _on_sync_completed

        despachos: list[dict] = []
        monkeypatch.setattr(
            analytics_tasks.rebuild_after_sync,
            "delay",
            lambda **kw: despachos.append(kw),
        )
        _on_sync_completed(
            sender=None,
            organization=organization_a,
            capability="INVOICES",
            records_processed=0,
        )
        assert despachos == []
