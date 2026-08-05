"""Testes da sessão de 3 dias com renovação deslizante (#85).

Cobre os settings resolvidos, o override por env var (`SESSION_COOKIE_AGE_DAYS`)
e o fato de que logout continua invalidando a sessão na hora.
"""

from __future__ import annotations

import pytest
from django.conf import settings
from django.contrib.sessions.models import Session
from django.test import Client
from django.urls import reverse

from apps.tenancy.models import User

THREE_DAYS_IN_SECONDS = 60 * 60 * 24 * 3


class TestSessionSettings:
    """Settings resolvidos — o que o Django efetivamente usa em runtime."""

    def test_cookie_age_is_three_days(self) -> None:
        assert settings.SESSION_COOKIE_AGE == THREE_DAYS_IN_SECONDS

    def test_does_not_expire_at_browser_close(self) -> None:
        assert settings.SESSION_EXPIRE_AT_BROWSER_CLOSE is False

    def test_saves_every_request(self) -> None:
        """Janela deslizante: cada request renova a expiração."""
        assert settings.SESSION_SAVE_EVERY_REQUEST is True

    def test_hardening_preservado(self) -> None:
        """A issue amplia a duração, mas não afrouxa o resto do cookie."""
        assert settings.SESSION_COOKIE_HTTPONLY is True
        assert settings.SESSION_COOKIE_SAMESITE == "Lax"


class TestSessionAgeEnvVar:
    """`SESSION_COOKIE_AGE_DAYS` permite ajustar sem redeploy de código."""

    def _settings_cls(self):
        from config.settings._env import Settings

        return Settings

    def _required_kwargs(self) -> dict[str, str]:
        return {
            "DJANGO_SECRET_KEY": "test-only-not-a-real-secret",
            "DATABASE_URL": "postgres://u:p@localhost:5432/db",
            "FERNET_KEY": "change-me-generate-with-fernet",
        }

    def test_default_is_three_days(self) -> None:
        env = self._settings_cls()(**self._required_kwargs())
        assert env.SESSION_COOKIE_AGE_DAYS == 3

    def test_env_var_overrides_default(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("SESSION_COOKIE_AGE_DAYS", "7")
        env = self._settings_cls()(**self._required_kwargs())
        assert env.SESSION_COOKIE_AGE_DAYS == 7
        assert 60 * 60 * 24 * env.SESSION_COOKIE_AGE_DAYS == 604800

    def test_rejects_zero_days(self) -> None:
        """Zero/negativo mataria a sessão na hora — falha cedo, no import."""
        from pydantic import ValidationError

        with pytest.raises(ValidationError):
            self._settings_cls()(SESSION_COOKIE_AGE_DAYS=0, **self._required_kwargs())


@pytest.mark.django_db
class TestLogoutInvalidaSessao:
    """Sessão mais longa não pode enfraquecer o logout."""

    def test_logout_apaga_sessao_do_banco(self, user_a: User) -> None:
        client = Client()
        client.force_login(user_a)
        session_key = client.session.session_key
        assert session_key is not None
        assert Session.objects.filter(session_key=session_key).exists()

        response = client.post(reverse("account_logout"))

        assert response.status_code in (200, 302)
        assert not Session.objects.filter(session_key=session_key).exists()
        assert "_auth_user_id" not in client.session
