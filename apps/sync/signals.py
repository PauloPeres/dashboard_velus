"""Signals do bounded context Sync.

`sync_completed`: emitido após `sync_capability` finalizar com sucesso.
Outros bounded contexts (analytics) escutam pra recomputar fact tables agregadas.
"""

from __future__ import annotations

import django.dispatch

# providing_args removido no Django 4.0; documentar via type hint do receiver.
# Receivers esperam: sender=None, organization, source_type, capability, records_processed
sync_completed = django.dispatch.Signal()
