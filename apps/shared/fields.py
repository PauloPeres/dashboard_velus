"""Campos customizados — EncryptedField para credenciais sensíveis."""

from __future__ import annotations

from typing import Any

from cryptography.fernet import Fernet, InvalidToken
from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import models


class EncryptedTextField(models.BinaryField):
    """Campo de texto criptografado em repouso via Fernet (AES-128-CBC + HMAC).

    Armazenado como bytes no DB. Aplicação manipula como str.

    Uso:
        class OrganizationDataSource(models.Model):
            credentials_encrypted = EncryptedTextField()

    A key vem de `settings.FERNET_KEY` (lida de env var FERNET_KEY).
    Em produção: K8s Secret separado do Postgres password. Rotação documentada
    no runbook (gerar nova key, rerodar dual-write, descontinuar key antiga).

    NÃO usar pra dados searchable — busca por valor descriptografado não é viável.
    Pra dados searchable + sigilosos, prefira hash determinístico (HMAC) em campo
    separado e cripto só do plaintext.
    """

    description = "Campo de texto criptografado via Fernet"

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        # BinaryField default tem editable=False; aqui queremos editável (set por código)
        kwargs.setdefault("editable", True)
        super().__init__(*args, **kwargs)

    def _fernet(self) -> Fernet:
        key = settings.FERNET_KEY
        if isinstance(key, str):
            key = key.encode()
        return Fernet(key)

    def from_db_value(
        self,
        value: bytes | memoryview | None,
        expression: Any,  # noqa: ARG002
        connection: Any,  # noqa: ARG002
    ) -> str | None:
        if value is None:
            return None
        return self._decrypt(value)

    def to_python(self, value: Any) -> str | None:
        if value is None:
            return None
        if isinstance(value, str):
            return value
        return self._decrypt(value)

    def get_prep_value(self, value: Any) -> bytes | None:
        if value is None:
            return None
        if isinstance(value, bytes):
            # Já criptografado — passou por ciclo to_python? Não rerencriptar.
            return value
        if not isinstance(value, str):
            raise ValidationError(
                f"EncryptedTextField espera str ou None, recebeu {type(value).__name__}"
            )
        return self._encrypt(value)

    def _encrypt(self, plaintext: str) -> bytes:
        return self._fernet().encrypt(plaintext.encode("utf-8"))

    def _decrypt(self, ciphertext: bytes | memoryview) -> str:
        if isinstance(ciphertext, memoryview):
            ciphertext = bytes(ciphertext)
        try:
            return self._fernet().decrypt(ciphertext).decode("utf-8")
        except InvalidToken as exc:
            raise ValidationError(
                "Falha ao descriptografar — FERNET_KEY pode ter sido rotacionada "
                "sem migrar dados antigos."
            ) from exc
