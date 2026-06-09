"""Testes e2e do bounded context Atendimento.

Cobre o Repository (upsert idempotente, FK por documento, FK de departamento) e
a orquestracao `run_opa_sync` ponta a ponta com FakeAtendimentoSource:
mapa cliente->documento, persistencia, mensagens opcionais, incremental e
isolamento por organizacao.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from apps.atendimento.application.sync import run_opa_sync
from apps.atendimento.domain.dto import (
    AtendenteRefDTO,
    AtendimentoDTO,
    ClienteRefDTO,
    DepartamentoDTO,
    MensagemDTO,
)
from apps.atendimento.infrastructure.models import (
    Atendimento,
    Departamento,
    Mensagem,
)
from apps.atendimento.infrastructure.repositories import (
    AtendimentoRepository,
    DepartamentoRepository,
)
from apps.atendimento.tasks import sync_opa_for_all_orgs
from apps.customers.infrastructure.models import Customer
from apps.integrations.fake.atendimento import FakeAtendimentoSource
from apps.integrations.shared.enums import Capability, SourceType
from apps.shared.context import set_current_organization
from apps.sync.models import SyncCheckpoint
from apps.tenancy.models import Organization, OrganizationDataSource


def _make_customer(org: Organization, *, document: str, name: str) -> Customer:
    set_current_organization(org)
    return Customer.objects.create(
        organization=org,
        source_type=SourceType.IXC.value,
        external_id=f"ixc-{document}",
        document=document,
        name=name,
        status=Customer.Status.ACTIVE.value,
    )


def _atendimento_dto(**overrides: object) -> AtendimentoDTO:
    base = {
        "external_id": "a1",
        "customer_external_id": "cli-opaco-1",
        "customer_document": "",
        "customer_name": "Bruna",
        "departamento_external_id": "dep-suporte",
        "atendente_external_id": "u9",
        "atendente_nome": "",
        "status": "CLOSED",
        "canal": "whatsapp",
        "protocol": "OPA202301",
        "opened_at": datetime(2023, 1, 10, 12, 0, tzinfo=UTC),
    }
    base.update(overrides)
    return AtendimentoDTO(**base)  # type: ignore[arg-type]


# =============================================================================
# Repository — upsert idempotente, FK por documento e por departamento
# =============================================================================
@pytest.mark.django_db
class TestAtendimentoRepository:
    def test_creates_atendimento(self, organization_a: Organization) -> None:
        set_current_organization(organization_a)
        repo = AtendimentoRepository(organization_a)
        at, created = repo.upsert_from_dto(
            _atendimento_dto(), source_type=SourceType.OPA
        )
        assert created is True
        assert at.external_id == "a1"
        assert at.status == "CLOSED"
        assert at.canal == "whatsapp"

    def test_upsert_idempotent_no_duplicate(self, organization_a: Organization) -> None:
        set_current_organization(organization_a)
        repo = AtendimentoRepository(organization_a)
        repo.upsert_from_dto(_atendimento_dto(), source_type=SourceType.OPA)
        _, created = repo.upsert_from_dto(
            _atendimento_dto(status="IN_PROGRESS"), source_type=SourceType.OPA
        )
        assert created is False
        set_current_organization(organization_a)
        assert Atendimento.objects.count() == 1
        assert Atendimento.objects.get(external_id="a1").status == "IN_PROGRESS"

    def test_resolves_customer_fk_by_document(
        self, organization_a: Organization
    ) -> None:
        _make_customer(organization_a, document="12345678901", name="Bruna Carvalho")
        set_current_organization(organization_a)
        repo = AtendimentoRepository(organization_a)
        at, _ = repo.upsert_from_dto(
            _atendimento_dto(customer_document="123.456.789-01"),
            source_type=SourceType.OPA,
        )
        assert at.customer is not None
        assert at.customer.name == "Bruna Carvalho"
        assert at.customer_document == "12345678901"  # normalizado

    def test_null_customer_when_document_absent(
        self, organization_a: Organization
    ) -> None:
        set_current_organization(organization_a)
        repo = AtendimentoRepository(organization_a)
        at, _ = repo.upsert_from_dto(
            _atendimento_dto(customer_document=""), source_type=SourceType.OPA
        )
        assert at.customer is None

    def test_resolves_departamento_fk(self, organization_a: Organization) -> None:
        set_current_organization(organization_a)
        dep_repo = DepartamentoRepository(organization_a)
        dep_repo.upsert_from_dto(
            DepartamentoDTO(external_id="dep-suporte", nome="Suporte"),
            source_type=SourceType.OPA,
        )
        repo = AtendimentoRepository(organization_a)
        at, _ = repo.upsert_from_dto(_atendimento_dto(), source_type=SourceType.OPA)
        assert at.departamento is not None
        assert at.departamento.nome == "Suporte"

    def test_unknown_status_normalized(self, organization_a: Organization) -> None:
        set_current_organization(organization_a)
        repo = AtendimentoRepository(organization_a)
        at, _ = repo.upsert_from_dto(
            _atendimento_dto(status="WHATEVER"), source_type=SourceType.OPA
        )
        assert at.status == Atendimento.Status.UNKNOWN.value


# =============================================================================
# run_opa_sync — pipeline completa com FakeAtendimentoSource
# =============================================================================
@pytest.mark.django_db
@pytest.mark.e2e
class TestOpaSyncE2E:
    def _seed(self) -> None:
        FakeAtendimentoSource.set_seed(
            departamentos=[
                DepartamentoDTO(external_id="dep-suporte", nome="Suporte", status="A"),
            ],
            clientes=[
                ClienteRefDTO(
                    external_id="cli-opaco-1", document="12345678901", nome="Bruna"
                ),
            ],
            atendentes=[
                AtendenteRefDTO(external_id="u9", nome="Felipe"),
            ],
            atendimentos=[
                _atendimento_dto(
                    external_id="a1",
                    customer_external_id="cli-opaco-1",
                    opened_at=datetime(2023, 1, 10, 12, 0, tzinfo=UTC),
                ),
                _atendimento_dto(
                    external_id="a2",
                    customer_external_id="cli-desconhecido",
                    opened_at=datetime(2023, 1, 20, 12, 0, tzinfo=UTC),
                ),
            ],
            mensagens={
                "a1": [
                    MensagemDTO(
                        external_id="m1",
                        atendimento_external_id="a1",
                        direction="CLIENT",
                        tipo="texto",
                        texto="oi",
                        sent_at=datetime(2023, 1, 10, 12, 1, tzinfo=UTC),
                    ),
                ],
            },
        )

    def test_sync_persists_and_links_customer_by_document(
        self, organization_a: Organization
    ) -> None:
        _make_customer(organization_a, document="12345678901", name="Bruna Carvalho")
        self._seed()

        result = run_opa_sync(organization_a, FakeAtendimentoSource())

        assert result.departamentos == 1
        assert result.atendimentos == 2
        assert result.customers_linked == 1  # só a1 casa por documento

        set_current_organization(organization_a)
        a1 = Atendimento.objects.get(external_id="a1")
        assert a1.customer is not None
        assert a1.customer.name == "Bruna Carvalho"
        assert a1.departamento is not None  # FK de departamento resolvida
        # nome do atendente resolvido pelo mapa id_opaco -> nome (u9 -> Felipe)
        assert a1.atendente_nome == "Felipe"
        a2 = Atendimento.objects.get(external_id="a2")
        assert a2.customer is None  # documento desconhecido

    def test_sync_idempotent_on_rerun(self, organization_a: Organization) -> None:
        self._seed()
        run_opa_sync(organization_a, FakeAtendimentoSource())
        run_opa_sync(organization_a, FakeAtendimentoSource())
        set_current_organization(organization_a)
        assert Atendimento.objects.count() == 2
        assert Departamento.objects.count() == 1

    def test_messages_only_when_flag_set(self, organization_a: Organization) -> None:
        self._seed()
        run_opa_sync(organization_a, FakeAtendimentoSource(), with_messages=False)
        set_current_organization(organization_a)
        assert Mensagem.objects.count() == 0

        run_opa_sync(organization_a, FakeAtendimentoSource(), with_messages=True)
        set_current_organization(organization_a)
        assert Mensagem.objects.count() == 1
        msg = Mensagem.objects.get(external_id="m1")
        assert msg.atendimento is not None  # FK pro atendimento resolvida
        assert msg.direction == "CLIENT"

    def test_incremental_since_filters_old(self, organization_a: Organization) -> None:
        self._seed()
        # since posterior a a1 (10/01) — só a2 (20/01) entra
        result = run_opa_sync(
            organization_a,
            FakeAtendimentoSource(),
            since=datetime(2023, 1, 15, tzinfo=UTC),
        )
        assert result.atendimentos == 1
        set_current_organization(organization_a)
        assert Atendimento.objects.count() == 1
        assert Atendimento.objects.first().external_id == "a2"

    def test_org_isolation(
        self, organization_a: Organization, organization_b: Organization
    ) -> None:
        self._seed()
        run_opa_sync(organization_a, FakeAtendimentoSource())
        set_current_organization(organization_b)
        assert Atendimento.objects.count() == 0
        set_current_organization(organization_a)
        assert Atendimento.objects.count() == 2


# =============================================================================
# sync_opa_for_all_orgs — task Celery agendada (recorrência dedicada do Opa!)
# =============================================================================
@pytest.mark.django_db
class TestOpaBeatTask:
    def _make_datasource(self, org: Organization) -> None:
        set_current_organization(org)
        ds = OrganizationDataSource.objects.create(
            organization=org,
            source_type=SourceType.OPA.value,
            capability=Capability.ATENDIMENTO.value,
            priority=100,
            is_active=True,
        )
        ds.set_credentials({"base_url": "https://opa.test", "token": "tok-xyz"})
        ds.save()

    def _seed(self) -> None:
        FakeAtendimentoSource.set_seed(
            departamentos=[
                DepartamentoDTO(external_id="dep-suporte", nome="Suporte", status="A"),
            ],
            clientes=[
                ClienteRefDTO(
                    external_id="cli-opaco-1", document="12345678901", nome="Bruna"
                ),
            ],
            atendimentos=[
                # Dentro da janela de 90 dias da task (since = now - 90d).
                _atendimento_dto(
                    external_id="a1",
                    opened_at=datetime.now(UTC) - timedelta(days=1),
                ),
            ],
        )

    def test_syncs_orgs_with_active_datasource(
        self, organization_a: Organization, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _make_customer(organization_a, document="12345678901", name="Bruna Carvalho")
        self._make_datasource(organization_a)
        self._seed()
        # OpaAtendimentoSource recebe base_url/token — FakeAtendimentoSource os ignora.
        monkeypatch.setattr(
            "apps.atendimento.tasks.OpaAtendimentoSource", FakeAtendimentoSource
        )

        result = sync_opa_for_all_orgs()

        assert result == {"orgs": 1, "atendimentos": 1}
        set_current_organization(organization_a)
        assert Atendimento.objects.count() == 1
        # Checkpoint avançou — próxima execução é incremental.
        cp = SyncCheckpoint.objects.get(
            source_type=SourceType.OPA.value,
            capability=Capability.ATENDIMENTO.value,
        )
        assert cp.last_processed_at is not None

    def test_skips_orgs_without_datasource(
        self, organization_a: Organization, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        self._seed()
        monkeypatch.setattr(
            "apps.atendimento.tasks.OpaAtendimentoSource", FakeAtendimentoSource
        )

        result = sync_opa_for_all_orgs()

        assert result == {"orgs": 0, "atendimentos": 0}
        set_current_organization(organization_a)
        assert Atendimento.objects.count() == 0
