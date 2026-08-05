"""Testes do transporte de e-mail via Mailgun (#95).

Cobre:
- resolução dos settings **sem** as variáveis (comportamento antigo preservado —
  é assim que dev local e CI rodam) e **com** elas (backend do anymail);
- redação da chave de API antes de qualquer log;
- convite logando o motivo da falha em vez de engolir a exceção;
- o command `send_test_email` (backend ativo no output, erro legível).

Nenhum teste manda e-mail de verdade: settings de teste forçam locmem e as
falhas são simuladas por mock.
"""

from __future__ import annotations

from io import StringIO
from typing import Any
from unittest.mock import patch

import pytest
from django.core import mail
from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import override_settings

from apps.shared.email import describe_backend, email_error_details, redact_secrets
from apps.tenancy.invites import invite_user
from apps.tenancy.models import Organization, User
from config.settings._email import MAILGUN_BACKEND, build_email_settings

CONSOLE = "django.core.mail.backends.console.EmailBackend"
FAKE_KEY = "key-naoehumachavereal00000000000000"  # dummy, não é chave real


def _build(**over: Any) -> dict[str, Any]:
    base = {
        "api_key": "",
        "sender_domain": "",
        "api_url": "https://api.mailgun.net/v3",
        "fallback_backend": CONSOLE,
        "default_from_email": "Velus Dashboard <noreply@velus.local>",
    }
    base.update(over)
    return build_email_settings(**base)


class TestResolucaoDeSettings:
    def test_sem_variaveis_mantem_backend_de_fallback(self) -> None:
        resolved = _build()
        assert resolved["EMAIL_ENABLED"] is False
        assert resolved["EMAIL_BACKEND"] == CONSOLE
        assert resolved["ANYMAIL"] == {}
        # remetente intocado — nada muda pra quem não tem a chave
        assert resolved["DEFAULT_FROM_EMAIL"] == "Velus Dashboard <noreply@velus.local>"
        assert resolved["SERVER_EMAIL"] == resolved["DEFAULT_FROM_EMAIL"]

    @pytest.mark.parametrize(
        ("api_key", "sender_domain"),
        [(FAKE_KEY, ""), ("", "seujaime.com"), ("   ", "seujaime.com")],
    )
    def test_configuracao_pela_metade_nao_liga(self, api_key: str, sender_domain: str) -> None:
        resolved = _build(api_key=api_key, sender_domain=sender_domain)
        assert resolved["EMAIL_ENABLED"] is False
        assert resolved["EMAIL_BACKEND"] == CONSOLE

    def test_com_variaveis_liga_backend_do_anymail(self) -> None:
        resolved = _build(api_key=FAKE_KEY, sender_domain="seujaime.com")
        assert resolved["EMAIL_ENABLED"] is True
        assert resolved["EMAIL_BACKEND"] == MAILGUN_BACKEND
        assert resolved["ANYMAIL"] == {
            "MAILGUN_API_KEY": FAKE_KEY,
            "MAILGUN_SENDER_DOMAIN": "seujaime.com",
            "MAILGUN_API_URL": "https://api.mailgun.net/v3",
        }

    def test_remetente_forcado_pro_dominio_verificado(self) -> None:
        """Mailgun recusa (403) remetente fora do domínio verificado."""
        resolved = _build(
            api_key=FAKE_KEY,
            sender_domain="seujaime.com",
            default_from_email="Velus <noreply@velus.seujaime.com>",
        )
        assert resolved["DEFAULT_FROM_EMAIL"] == "Velus Dashboard <noreply@seujaime.com>"
        assert resolved["SERVER_EMAIL"] == resolved["DEFAULT_FROM_EMAIL"]

    def test_remetente_ja_no_dominio_verificado_e_preservado(self) -> None:
        resolved = _build(
            api_key=FAKE_KEY,
            sender_domain="seujaime.com",
            default_from_email="Velus <contato@seujaime.com>",
        )
        assert resolved["DEFAULT_FROM_EMAIL"] == "Velus <contato@seujaime.com>"

    def test_api_url_vazia_cai_no_default_us(self) -> None:
        resolved = _build(api_key=FAKE_KEY, sender_domain="seujaime.com", api_url="")
        assert resolved["ANYMAIL"]["MAILGUN_API_URL"] == "https://api.mailgun.net/v3"

    def test_regiao_eu_e_respeitada(self) -> None:
        resolved = _build(
            api_key=FAKE_KEY,
            sender_domain="seujaime.com",
            api_url="https://api.eu.mailgun.net/v3",
        )
        assert resolved["ANYMAIL"]["MAILGUN_API_URL"] == "https://api.eu.mailgun.net/v3"


class TestSettingsDoAmbienteDeTeste:
    def test_teste_nunca_envia_de_verdade(self) -> None:
        from django.conf import settings

        assert settings.EMAIL_BACKEND.endswith("locmem.EmailBackend")
        assert settings.EMAIL_ENABLED is False


class _FakeResponse:
    status_code = 401
    text = f'{{"message": "Invalid private key {FAKE_KEY}"}}'


class _FakeAPIError(Exception):
    status_code = 401
    response = _FakeResponse()


class TestRedacaoEDetalhesDeErro:
    @override_settings(ANYMAIL={"MAILGUN_API_KEY": FAKE_KEY})
    def test_chave_nunca_aparece_no_detalhe_do_erro(self) -> None:
        exc = _FakeAPIError(f"Mailgun API 401 usando a chave {FAKE_KEY}")
        details = email_error_details(exc)
        assert FAKE_KEY not in details["error"]
        assert FAKE_KEY not in details["provider_response"]
        assert details["status_code"] == 401
        assert "Mailgun API 401" in details["error"]

    @override_settings(ANYMAIL={})
    def test_redacao_por_padrao_mesmo_sem_settings(self) -> None:
        assert FAKE_KEY not in redact_secrets(f"erro com {FAKE_KEY}")

    def test_erro_simples_sem_resposta_do_provedor(self) -> None:
        details = email_error_details(RuntimeError("conexão recusada"))
        assert details["error"] == "conexão recusada"
        assert "status_code" not in details
        assert "provider_response" not in details

    def test_describe_backend_deixa_claro_que_esta_desligado(self) -> None:
        assert "DESLIGADO" in describe_backend()

    @override_settings(
        EMAIL_ENABLED=True,
        EMAIL_BACKEND=MAILGUN_BACKEND,
        ANYMAIL={
            "MAILGUN_SENDER_DOMAIN": "seujaime.com",
            "MAILGUN_API_URL": "https://api.mailgun.net/v3",
        },
    )
    def test_describe_backend_mostra_dominio_sem_chave(self) -> None:
        descricao = describe_backend()
        assert "seujaime.com" in descricao
        assert "Mailgun" in descricao
        assert FAKE_KEY not in descricao


@pytest.mark.django_db
class TestConviteLogaFalhaDeEnvio:
    def test_falha_do_provedor_e_logada_com_motivo(self, organization_a: Organization) -> None:
        exc = _FakeAPIError("Mailgun API 401: Invalid private key")
        with (
            patch("allauth.account.forms.ResetPasswordForm.save", side_effect=exc),
            patch("apps.tenancy.invites._logger") as logger,
        ):
            res = invite_user(
                organization=organization_a,
                email="falha@empresa.com",
                role="MEMBER",
                access_group=None,
                invited_by=None,
            )

        # provisionamento não cai por causa do e-mail
        assert res.email_sent is False
        assert User.objects.filter(email="falha@empresa.com").exists()

        logger.exception.assert_called_once()
        _, kwargs = logger.exception.call_args
        assert kwargs["error_type"] == "_FakeAPIError"
        assert "Invalid private key" in kwargs["error"]
        assert kwargs["status_code"] == 401

    def test_sucesso_continua_enviando(self, organization_a: Organization) -> None:
        mail.outbox.clear()
        res = invite_user(
            organization=organization_a,
            email="ok@empresa.com",
            role="MEMBER",
            access_group=None,
            invited_by=None,
        )
        assert res.email_sent is True
        assert len(mail.outbox) == 1


class TestCommandSendTestEmail:
    def test_reporta_backend_ativo_e_envia(self) -> None:
        mail.outbox.clear()
        out = StringIO()
        call_command("send_test_email", "destino@exemplo.com", stdout=out, stderr=out)
        saida = out.getvalue()
        assert "Backend ativo" in saida
        assert "locmem" in saida
        # deixa explícito que nada foi entregue de verdade
        assert "DESLIGADO" in saida
        assert len(mail.outbox) == 1
        assert mail.outbox[0].to == ["destino@exemplo.com"]

    def test_destinatario_invalido(self) -> None:
        with pytest.raises(CommandError, match="Destinatário inválido"):
            call_command("send_test_email", "sem-arroba", stdout=StringIO())

    def test_erro_do_provedor_e_reportado_legivel(self) -> None:
        err = StringIO()
        out = StringIO()
        exc = _FakeAPIError("Mailgun API 403: Domain not verified")
        with (
            patch("django.core.mail.EmailMultiAlternatives.send", side_effect=exc),
            pytest.raises(CommandError, match="Envio falhou"),
        ):
            call_command("send_test_email", "destino@exemplo.com", stdout=out, stderr=err)
        saida = err.getvalue()
        assert "FALHA NO ENVIO" in saida
        assert "Domain not verified" in saida
        assert "status_code: 401" in saida
