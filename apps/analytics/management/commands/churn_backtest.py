"""Mede se os sinais de risco de churn separam quem cancela — issue #125.

Uso:
    python manage.py churn_backtest velus
    python manage.py churn_backtest velus --d0 2026-04-13 --horizonte 120

Somente leitura: reconstrói a base e o desfecho a partir de `Contract` e
compara a taxa de cancelamento de quem tinha cada sinal contra quem não tinha.
`lift` 1,0 é o acaso; abaixo disso o sinal aponta pro lado errado.

Ver `apps.analytics.application.churn_backtest` pro método e pras limitações —
em especial, quais sinais NÃO entram porque não têm história real.
"""

from __future__ import annotations

from datetime import date, timedelta

from django.core.management.base import BaseCommand, CommandError, CommandParser
from django.utils import timezone

from apps.analytics.application.churn_backtest import formatar, rodar_backtest
from apps.tenancy.models import Organization


class Command(BaseCommand):
    help = "Backtest dos sinais de risco de churn (read-only)."

    def add_arguments(self, parser: CommandParser) -> None:
        parser.add_argument("org_slug", help="slug da organização")
        parser.add_argument(
            "--d0",
            help="data de corte (YYYY-MM-DD). Padrão: hoje menos o horizonte",
        )
        parser.add_argument(
            "--horizonte",
            type=int,
            default=120,
            help="dias observados depois de D0 (padrão: 120)",
        )
        parser.add_argument(
            "--amostra-minima",
            type=int,
            default=15,
            help="ignora sinal com menos que isso (padrão: 15)",
        )

    def handle(self, **options: object) -> None:
        slug = str(options["org_slug"])
        try:
            org = Organization.objects.get(slug=slug)
        except Organization.DoesNotExist as exc:
            raise CommandError(f"Organização '{slug}' não encontrada") from exc

        horizonte = int(options["horizonte"])  # type: ignore[arg-type]
        if options.get("d0"):
            try:
                d0 = date.fromisoformat(str(options["d0"]))
            except ValueError as exc:
                raise CommandError("--d0 precisa ser YYYY-MM-DD") from exc
        else:
            d0 = timezone.now().date() - timedelta(days=horizonte)

        if d0 >= timezone.now().date():
            raise CommandError("D0 precisa estar no passado")

        resultado = rodar_backtest(
            org,
            d0=d0,
            horizonte=horizonte,
            amostra_minima=int(options["amostra_minima"]),  # type: ignore[arg-type]
        )
        self.stdout.write(formatar(resultado))
