"""Popula DB local com dados sintéticos pra demonstrar dashboards sem credenciais IXC.

Uso:
    python manage.py seed_demo_data velus --customers=500 --months=12

Idempotente: re-rodar não duplica, atualiza/reusa registros existentes.
NÃO usar em produção — só ambiente dev/demo.
"""

from __future__ import annotations

import random
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any

from django.core.management.base import BaseCommand, CommandError, CommandParser
from django.db import transaction
from django.utils import timezone

from apps.analytics.application.rebuild import rebuild_for_capability
from apps.customers.infrastructure.models import Contract, Customer
from apps.financial.infrastructure.models import Invoice, Payment
from apps.integrations.shared.enums import SourceType
from apps.shared.context import set_current_organization
from apps.shared.decorators import allow_cross_tenant
from apps.tenancy.models import Organization

_FIRST_NAMES = [
    "Bruna", "Carlos", "Ana", "João", "Maria", "Pedro", "Juliana", "Rafael",
    "Camila", "Lucas", "Fernanda", "Roberto", "Patrícia", "Marcos", "Larissa",
    "Bruno", "Beatriz", "Diego", "Sofia", "Eduardo", "Letícia", "Gustavo",
    "Mariana", "Felipe", "Carolina", "Thiago", "Renata", "André", "Vanessa",
    "Ricardo", "Aline", "Sérgio", "Daniela", "Vitor", "Tatiana",
]
_LAST_NAMES = [
    "Silva", "Santos", "Souza", "Oliveira", "Pereira", "Ferreira", "Rodrigues",
    "Lima", "Costa", "Carvalho", "Almeida", "Mendes", "Ribeiro", "Martins",
    "Gomes", "Araújo", "Barbosa", "Cardoso", "Dias", "Nascimento",
]
_PLANS = [
    ("Fibra 50M", Decimal("70.00")),
    ("Fibra 200M", Decimal("100.00")),
    ("Fibra 500M", Decimal("150.00")),
    ("Fibra 1GB", Decimal("220.00")),
]
_DEMO_SOURCE = SourceType.FAKE.value


class Command(BaseCommand):
    help = "Popula DB com dados sintéticos pra demo dos dashboards."

    def add_arguments(self, parser: CommandParser) -> None:
        parser.add_argument("org_slug", type=str, help="Slug da Organization (deve existir)")
        parser.add_argument("--customers", type=int, default=500)
        parser.add_argument("--months", type=int, default=12)
        parser.add_argument("--seed", type=int, default=42)

    @allow_cross_tenant(reason="seed_demo_data popula dados pra demo")
    def handle(self, *args: Any, **opts: Any) -> None:  # noqa: ARG002
        org_slug: str = opts["org_slug"]
        n_customers: int = opts["customers"]
        months: int = opts["months"]
        random.seed(opts["seed"])

        try:
            org = Organization.objects.get(slug=org_slug)
        except Organization.DoesNotExist as exc:
            raise CommandError(
                f"Organization '{org_slug}' não existe. "
                f"Rode `create_organization {org_slug} ...` primeiro."
            ) from exc

        set_current_organization(org)
        self.stdout.write(self.style.SUCCESS(f"Populando '{org.slug}' com dados sintéticos..."))

        with transaction.atomic():
            customers = self._seed_customers(org, n_customers)
            self.stdout.write(f"  ✓ {len(customers)} customers")
            contracts = self._seed_contracts(org, customers, months)
            self.stdout.write(f"  ✓ {len(contracts)} contracts")
            invoices, payments = self._seed_invoices_and_payments(org, contracts, months)
            self.stdout.write(f"  ✓ {len(invoices)} invoices")
            self.stdout.write(f"  ✓ {len(payments)} payments")

        # Rebuild fact tables
        self.stdout.write("\nRebuild de fact tables...")
        for cap in ("CUSTOMERS", "CONTRACTS", "INVOICES", "PAYMENTS"):
            summary = rebuild_for_capability(org, cap)
            self.stdout.write(f"  {cap}: {summary}")

        self.stdout.write(self.style.SUCCESS("\n✓ Seed completo. Abrir dashboards em /"))

    # -------------------------------------------------------------------------
    # Geradores
    # -------------------------------------------------------------------------
    def _seed_customers(self, org: Organization, n: int) -> list[Customer]:
        result: list[Customer] = []
        base_date = timezone.now() - timedelta(days=400)
        for i in range(1, n + 1):
            ext_id = f"demo-cust-{i}"
            first = random.choice(_FIRST_NAMES)
            last = random.choice(_LAST_NAMES)
            name = f"{first} {last}"
            doc = "".join(str(random.randint(0, 9)) for _ in range(11))
            created = base_date + timedelta(days=random.randint(0, 400))
            customer, _ = Customer.objects.update_or_create(
                organization=org,
                source_type=_DEMO_SOURCE,
                external_id=ext_id,
                defaults={
                    "document": doc,
                    "name": name,
                    "email": f"{first.lower()}.{last.lower()}@demo.local",
                    "phone": f"14{random.randint(900000000, 999999999)}",
                    "status": "ACTIVE",
                    "created_at_source": created,
                },
            )
            result.append(customer)
        return result

    def _seed_contracts(
        self, org: Organization, customers: list[Customer], months: int
    ) -> list[Contract]:
        result: list[Contract] = []
        now = timezone.now()
        # ~5% churn ao longo do período
        churned_idx = set(random.sample(range(len(customers)), max(1, len(customers) // 20)))
        for i, cust in enumerate(customers):
            plan_name, monthly = random.choice(_PLANS)
            activated_offset = random.randint(0, months * 30)
            activated_at = now - timedelta(days=activated_offset)
            canceled_at = None
            status = "ACTIVE"
            if i in churned_idx:
                canceled_at = activated_at + timedelta(days=random.randint(30, 300))
                if canceled_at > now:
                    canceled_at = None
                    status = "ACTIVE"
                else:
                    status = "CANCELED"
            elif random.random() < 0.05:
                status = "BLOCKED"

            contract, _ = Contract.objects.update_or_create(
                organization=org,
                source_type=_DEMO_SOURCE,
                external_id=f"demo-ctr-{cust.external_id}",
                defaults={
                    "customer": cust,
                    "customer_external_id": cust.external_id,
                    "plan_name": plan_name,
                    "monthly_amount": monthly,
                    "status": status,
                    "activated_at": activated_at,
                    "canceled_at": canceled_at,
                },
            )
            result.append(contract)
        return result

    def _seed_invoices_and_payments(
        self,
        org: Organization,
        contracts: list[Contract],
        months: int,  # noqa: ARG002 — reservado pra cap por janela futura
    ) -> tuple[list[Invoice], list[Payment]]:
        invoices: list[Invoice] = []
        payments: list[Payment] = []
        now = timezone.now()
        today = now.date()

        for ctr in contracts:
            if not ctr.activated_at:
                continue
            start = ctr.activated_at.date()
            end = ctr.canceled_at.date() if ctr.canceled_at else today
            if end > today:
                end = today

            # Gera 1 fatura por mês desde ativação
            month_cursor = date(start.year, start.month, 10)  # vencimento dia 10
            invoice_idx = 0
            while month_cursor <= end:
                invoice_idx += 1
                inv_ext = f"demo-inv-{ctr.external_id}-{invoice_idx}"
                paid_at: datetime | None = None
                paid_amount: Decimal | None = None
                status = "PENDING"
                is_past_due = month_cursor < today

                if is_past_due:
                    # 92% pagamentos no prazo, 5% atraso, 3% inadimplente
                    r = random.random()
                    if r < 0.92:
                        paid_offset = random.randint(-5, 5)
                        paid_at_date = month_cursor + timedelta(days=paid_offset)
                        paid_at = datetime.combine(paid_at_date, datetime.min.time(), tzinfo=UTC)
                        paid_amount = ctr.monthly_amount
                        status = "PAID"
                    elif r < 0.97:
                        paid_offset = random.randint(10, 45)
                        paid_at_date = month_cursor + timedelta(days=paid_offset)
                        if paid_at_date <= today:
                            paid_at = datetime.combine(
                                paid_at_date, datetime.min.time(), tzinfo=UTC
                            )
                            paid_amount = ctr.monthly_amount + Decimal("5.00")
                            status = "PAID"
                        else:
                            status = "OVERDUE"
                    else:
                        status = "OVERDUE"

                inv, _ = Invoice.objects.update_or_create(
                    organization=org,
                    source_type=_DEMO_SOURCE,
                    external_id=inv_ext,
                    defaults={
                        "contract": ctr,
                        "contract_external_id": ctr.external_id,
                        "amount": ctr.monthly_amount,
                        "due_date": month_cursor,
                        "status": status,
                        "issued_at": datetime.combine(
                            month_cursor - timedelta(days=10),
                            datetime.min.time(),
                            tzinfo=UTC,
                        ),
                        "paid_at": paid_at,
                        "paid_amount": paid_amount,
                    },
                )
                invoices.append(inv)

                # Se pago, registra pagamento associado
                if paid_at is not None:
                    pay_ext = f"demo-pay-{inv_ext}"
                    method = random.choice(["BOLETO", "PIX", "PIX", "PIX", "CARD"])
                    pay, _ = Payment.objects.update_or_create(
                        organization=org,
                        source_type=_DEMO_SOURCE,
                        external_id=pay_ext,
                        defaults={
                            "invoice": inv,
                            "contract": ctr,
                            "invoice_external_id": inv_ext,
                            "contract_external_id": ctr.external_id,
                            "amount": paid_amount or ctr.monthly_amount,
                            "paid_at": paid_at,
                            "method": method,
                        },
                    )
                    payments.append(pay)

                # Avança 1 mês
                next_month = month_cursor.month + 1
                next_year = month_cursor.year
                if next_month > 12:
                    next_month = 1
                    next_year += 1
                month_cursor = date(next_year, next_month, 10)

        return invoices, payments
