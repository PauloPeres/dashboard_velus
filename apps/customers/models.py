"""Re-export — Django descobre models pelo módulo `models.py` raiz do app.

Models concretos vivem em `infrastructure/models.py` (separação DDD), mas
Django precisa achá-los aqui pra rodar makemigrations/migrate.
"""

from __future__ import annotations

from .infrastructure.models import Customer

__all__ = ("Customer",)
