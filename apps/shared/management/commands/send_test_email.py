"""Envia um e-mail de teste e reporta o resultado (#95).

É a forma de validar produção sem convidar uma pessoa de verdade:

    kubectl exec -n dashboard-velus deploy/web -- \
        python manage.py send_test_email paulo@exemplo.com

O output diz **qual backend está ativo** antes de tentar — se aparecer
`console`/`locmem`, o Secret do Mailgun não chegou no pod e nada foi entregue,
por mais que o comando termine "com sucesso".

Erro do provedor (chave inválida, domínio não verificado, destinatário
recusado) é impresso legível, com status HTTP e corpo da resposta do Mailgun.
"""

from __future__ import annotations

from typing import Any

from django.conf import settings
from django.core.mail import EmailMultiAlternatives
from django.core.management.base import BaseCommand, CommandError, CommandParser
from django.utils import timezone

from apps.shared.email import describe_backend, email_error_details


class Command(BaseCommand):
    help = "Envia um e-mail de teste pro destinatário informado e reporta o resultado."

    def add_arguments(self, parser: CommandParser) -> None:
        parser.add_argument("destinatario", type=str, help="E-mail de destino")
        parser.add_argument(
            "--subject",
            type=str,
            default="[Velus] Teste de envio de e-mail",
            help="Assunto do e-mail de teste.",
        )

    def handle(self, *args: Any, **opts: Any) -> None:  # noqa: ARG002
        destinatario: str = opts["destinatario"].strip()
        if "@" not in destinatario:
            raise CommandError(f"Destinatário inválido: {destinatario!r}")

        enabled = getattr(settings, "EMAIL_ENABLED", False)
        self.stdout.write(f"Backend ativo : {describe_backend()}")
        self.stdout.write(f"Remetente     : {settings.DEFAULT_FROM_EMAIL}")
        self.stdout.write(f"Destinatário  : {destinatario}")
        if not enabled:
            self.stdout.write(
                self.style.WARNING(
                    "AVISO: envio real desligado (MAILGUN_API_KEY/MAILGUN_SENDER_DOMAIN "
                    "ausentes). O e-mail NÃO vai chegar na caixa de ninguém."
                )
            )

        agora = timezone.localtime().strftime("%d/%m/%Y %H:%M:%S")
        corpo = (
            "Este é um e-mail de teste do Velus Dashboard.\n\n"
            f"Enviado em: {agora}\n"
            f"Backend: {settings.EMAIL_BACKEND}\n\n"
            "Se você recebeu isto, o transporte de e-mail está funcionando."
        )
        msg = EmailMultiAlternatives(
            subject=opts["subject"],
            body=corpo,
            from_email=settings.DEFAULT_FROM_EMAIL,
            to=[destinatario],
        )

        try:
            sent = msg.send()
        except Exception as exc:
            detalhes = email_error_details(exc)
            self.stderr.write(self.style.ERROR(f"FALHA NO ENVIO ({type(exc).__name__})"))
            for chave, valor in detalhes.items():
                self.stderr.write(f"  {chave}: {valor}")
            raise CommandError("Envio falhou — veja o erro do provedor acima.") from exc

        if not sent:
            raise CommandError("Backend aceitou a chamada mas reportou 0 mensagens enviadas.")

        status = getattr(msg, "anymail_status", None)
        if status is not None:
            self.stdout.write(f"Status Mailgun: {status.status}")
            if status.message_id:
                self.stdout.write(f"Message-ID    : {status.message_id}")

        self.stdout.write(self.style.SUCCESS(f"Enviado ({sent} mensagem(ns))."))
        if not enabled:
            self.stdout.write(
                self.style.WARNING(
                    "Lembrete: com o backend atual, 'enviado' significa apenas "
                    "impresso/armazenado localmente."
                )
            )
