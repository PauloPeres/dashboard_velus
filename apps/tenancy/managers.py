"""Custom manager para o User custom (autenticação por email, sem username)."""

from __future__ import annotations

from typing import Any

from django.contrib.auth.hashers import make_password
from django.contrib.auth.models import BaseUserManager
from django.db.models import Manager


class UserManager(BaseUserManager["User"]):  # type: ignore[name-defined]
    """Manager onde `email` é o identificador único.

    Substitui o `UserManager` padrão do Django (que usa username).
    """

    use_in_migrations = True

    def _create_user(self, email: str, password: str | None, **extra_fields: Any) -> User:  # type: ignore[name-defined]  # noqa: F821
        if not email:
            raise ValueError("Email é obrigatório.")
        email = self.normalize_email(email)
        user = self.model(email=email, **extra_fields)
        user.password = make_password(password)
        user.save(using=self._db)
        return user

    def create_user(
        self, email: str, password: str | None = None, **extra_fields: Any
    ) -> User:  # type: ignore[name-defined]  # noqa: F821
        extra_fields.setdefault("is_staff", False)
        extra_fields.setdefault("is_superuser", False)
        return self._create_user(email, password, **extra_fields)

    def create_superuser(
        self, email: str, password: str | None = None, **extra_fields: Any
    ) -> User:  # type: ignore[name-defined]  # noqa: F821
        extra_fields.setdefault("is_staff", True)
        extra_fields.setdefault("is_superuser", True)

        if extra_fields.get("is_staff") is not True:
            raise ValueError("Superuser precisa de is_staff=True.")
        if extra_fields.get("is_superuser") is not True:
            raise ValueError("Superuser precisa de is_superuser=True.")

        return self._create_user(email, password, **extra_fields)


# Re-export do Manager base para uso explícito (sem filtro por tenant) onde necessário.
PlainManager = Manager
