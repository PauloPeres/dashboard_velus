"""Models de persistencia do bounded context Atendimento.

Herda `apps.shared.TenantModel` -> ganha `organization` FK indexada + TenantManager.
Identidade composta: `(organization, source_type, external_id)` e unique.

Atendimento liga-se ao Customer por **documento (CPF/CNPJ)**, resolvido no
Repository, pois o id de cliente da fonte (Opa) nao bate com o external_id do
Customer (IXC). Por isso guardamos tanto a FK (quando resolvida) quanto o
`customer_document` snapshot.
"""

from __future__ import annotations

from django.conf import settings
from django.db import models
from django.utils.translation import gettext_lazy as _
from simple_history.models import HistoricalRecords

from apps.integrations.shared.enums import SourceType
from apps.shared.models import TenantModel


class Departamento(TenantModel):
    """Setor/departamento de atendimento (Comercial, Suporte, Triagem, ...)."""

    source_type = models.CharField(
        max_length=32,
        choices=SourceType.choices,
        help_text=_("Sistema externo que originou este registro."),
    )
    external_id = models.CharField(
        max_length=128,
        help_text=_("ID do setor no sistema externo (opaco — string)."),
    )
    nome = models.CharField(max_length=255, blank=True, default="")
    status = models.CharField(max_length=32, blank=True, default="")

    raw_extras = models.JSONField(default=dict, blank=True)

    class Meta:
        verbose_name = _("Departamento de atendimento")
        verbose_name_plural = _("Departamentos de atendimento")
        constraints = [
            models.UniqueConstraint(
                fields=["organization", "source_type", "external_id"],
                name="unique_atendimento_departamento_per_source",
            ),
        ]
        indexes = [
            models.Index(fields=["organization", "source_type"]),
        ]

    def __str__(self) -> str:
        return f"{self.nome} ({self.source_type}:{self.external_id})"


class Motivo(TenantModel):
    """Motivo configurado de atendimento (catalogo idMotivo -> nome).

    Espelha `Etiqueta`: catalogo barato sincronizado da fonte (`atendimento/motivo`)
    usado pra resolver o nome dos motivos que a listagem de atendimentos so traz
    como id opaco (`idMotivo`)."""

    source_type = models.CharField(
        max_length=32,
        choices=SourceType.choices,
        help_text=_("Sistema externo que originou este registro."),
    )
    external_id = models.CharField(
        max_length=128,
        help_text=_("ID do motivo no sistema externo (idMotivo opaco — string)."),
    )
    nome = models.CharField(max_length=255, blank=True, default="")

    raw_extras = models.JSONField(default=dict, blank=True)

    class Meta:
        verbose_name = _("Motivo de atendimento")
        verbose_name_plural = _("Motivos de atendimento")
        constraints = [
            models.UniqueConstraint(
                fields=["organization", "source_type", "external_id"],
                name="unique_atendimento_motivo_per_source",
            ),
        ]
        indexes = [
            models.Index(fields=["organization", "source_type"]),
        ]

    def __str__(self) -> str:
        return f"{self.nome} ({self.source_type}:{self.external_id})"


class Etiqueta(TenantModel):
    """Etiqueta/tag configurada de atendimento (catalogo id_tag -> nome).

    Espelha `Departamento`: catalogo barato sincronizado da fonte (Opa! Suite,
    ~dezenas de registros) usado pra resolver o nome das tags que a listagem de
    atendimentos so traz como id opaco (`id_tag`)."""

    source_type = models.CharField(
        max_length=32,
        choices=SourceType.choices,
        help_text=_("Sistema externo que originou este registro."),
    )
    external_id = models.CharField(
        max_length=128,
        help_text=_("ID da etiqueta no sistema externo (id_tag opaco — string)."),
    )
    nome = models.CharField(max_length=255, blank=True, default="")
    cor = models.CharField(max_length=32, blank=True, default="")

    raw_extras = models.JSONField(default=dict, blank=True)

    class Meta:
        verbose_name = _("Etiqueta de atendimento")
        verbose_name_plural = _("Etiquetas de atendimento")
        constraints = [
            models.UniqueConstraint(
                fields=["organization", "source_type", "external_id"],
                name="unique_atendimento_etiqueta_per_source",
            ),
        ]
        indexes = [
            models.Index(fields=["organization", "source_type"]),
        ]

    def __str__(self) -> str:
        return f"{self.nome} ({self.source_type}:{self.external_id})"


class Atendimento(TenantModel):
    """Atendimento/conversa omnichannel vindo de uma fonte externa (Opa! Suite, ...).

    `customer` e FK opcional pq o vinculo depende de o documento da conversa
    casar com um Customer ja sincronizado. Repository resolve via
    `(organization, document)` no upsert; `customer_document` guarda o snapshot.
    """

    class Status(models.TextChoices):
        OPEN = "OPEN", _("Aberto")
        IN_PROGRESS = "IN_PROGRESS", _("Em atendimento")
        CLOSED = "CLOSED", _("Finalizado")
        UNKNOWN = "UNKNOWN", _("Desconhecido")

    source_type = models.CharField(
        max_length=32,
        choices=SourceType.choices,
        help_text=_("Sistema externo que originou este registro."),
    )
    external_id = models.CharField(
        max_length=128,
        help_text=_("ID do atendimento no sistema externo (opaco — string)."),
    )

    # Vinculo logico via documento (CPF/CNPJ), resolvido no Repository.
    customer = models.ForeignKey(
        "customers.Customer",
        on_delete=models.PROTECT,
        related_name="atendimentos",
        null=True,
        blank=True,
    )
    customer_external_id = models.CharField(
        max_length=128, blank=True, default="", db_index=True,
        help_text=_("ID opaco do cliente na fonte (nao bate com Customer.external_id)."),
    )
    customer_document = models.CharField(
        max_length=14, blank=True, default="", db_index=True,
        help_text=_("CPF/CNPJ normalizado — ponte logica pro Customer."),
    )
    customer_name = models.CharField(max_length=255, blank=True, default="")

    departamento = models.ForeignKey(
        "atendimento.Departamento",
        on_delete=models.PROTECT,
        related_name="atendimentos",
        null=True,
        blank=True,
    )
    departamento_external_id = models.CharField(
        max_length=128, blank=True, default="", db_index=True
    )

    atendente_external_id = models.CharField(max_length=128, blank=True, default="")
    atendente_nome = models.CharField(max_length=255, blank=True, default="")

    status = models.CharField(
        max_length=16, choices=Status.choices, default=Status.UNKNOWN
    )
    canal = models.CharField(max_length=64, blank=True, default="")
    protocol = models.CharField(max_length=128, blank=True, default="")

    motivos = models.JSONField(default=list, blank=True)
    # Nomes das etiquetas resolvidos via catalogo Etiqueta no sync (desnormalizado,
    # igual motivos). Um atendimento pode ter N tags. Fonte crua fica em raw_extras.
    tags = models.JSONField(default=list, blank=True)
    rating = models.PositiveSmallIntegerField(
        null=True, blank=True,
        help_text=_("Nota humana likert 1-5 (so vem em GET populado)."),
    )

    opened_at = models.DateTimeField(null=True, blank=True)
    closed_at = models.DateTimeField(null=True, blank=True)

    raw_extras = models.JSONField(default=dict, blank=True)

    history = HistoricalRecords()

    class Meta:
        verbose_name = _("Atendimento")
        verbose_name_plural = _("Atendimentos")
        constraints = [
            models.UniqueConstraint(
                fields=["organization", "source_type", "external_id"],
                name="unique_atendimento_per_source",
            ),
        ]
        indexes = [
            models.Index(fields=["organization", "status"]),
            models.Index(fields=["organization", "opened_at"]),
            models.Index(fields=["organization", "departamento_external_id"]),
            models.Index(fields=["organization", "customer_document"]),
        ]

    def __str__(self) -> str:
        return f"#{self.protocol} ({self.source_type}:{self.external_id})"


class Mensagem(TenantModel):
    """Mensagem trocada dentro de um Atendimento.

    Ingestao opcional/lazy (1 chamada por atendimento na fonte). FK pro
    atendimento resolvida via `(organization, source_type, atendimento_external_id)`.
    """

    class Direction(models.TextChoices):
        CLIENT = "CLIENT", _("Cliente")
        AGENT = "AGENT", _("Atendente")
        SYSTEM = "SYSTEM", _("Sistema")
        UNKNOWN = "UNKNOWN", _("Desconhecido")

    source_type = models.CharField(
        max_length=32,
        choices=SourceType.choices,
        help_text=_("Sistema externo que originou este registro."),
    )
    external_id = models.CharField(
        max_length=128,
        help_text=_("ID da mensagem no sistema externo (opaco — string)."),
    )

    atendimento = models.ForeignKey(
        "atendimento.Atendimento",
        on_delete=models.CASCADE,
        related_name="mensagens",
        null=True,
        blank=True,
    )
    atendimento_external_id = models.CharField(max_length=128, db_index=True)

    direction = models.CharField(
        max_length=16, choices=Direction.choices, default=Direction.UNKNOWN
    )
    tipo = models.CharField(max_length=64, blank=True, default="")
    texto = models.TextField(blank=True, default="")
    sent_at = models.DateTimeField(null=True, blank=True)

    raw_extras = models.JSONField(default=dict, blank=True)

    class Meta:
        verbose_name = _("Mensagem de atendimento")
        verbose_name_plural = _("Mensagens de atendimento")
        constraints = [
            models.UniqueConstraint(
                fields=["organization", "source_type", "external_id"],
                name="unique_atendimento_mensagem_per_source",
            ),
        ]
        indexes = [
            models.Index(
                fields=["organization", "source_type", "atendimento_external_id"]
            ),
            models.Index(fields=["organization", "sent_at"]),
        ]

    def __str__(self) -> str:
        return f"msg {self.source_type}:{self.external_id} ({self.direction})"


class EventoRede(TenantModel):
    """Evento de rede registrado **manualmente** (rompimento, manutencao, ...).

    Unico model do app que nao vem de fonte externa: e digitado na pagina de
    Tendencias de Atendimento (#78) pra dar contexto aos picos que a deteccao
    automatica de anomalia ja acha. Sem `source_type`/`external_id` justamente
    porque nao ha fonte — a identidade e o proprio registro do usuario.

    `ended_at` vazio = evento **pontual** (instante, nao intervalo).
    """

    class Tipo(models.TextChoices):
        ROMPIMENTO = "ROMPIMENTO", _("Rompimento")
        MANUTENCAO = "MANUTENCAO", _("Manutenção programada")
        QUEDA_LINK = "QUEDA_LINK", _("Queda de link")
        ATUALIZACAO = "ATUALIZACAO", _("Atualização")
        OUTRO = "OUTRO", _("Outro")

    # Cor de exibição por tipo — fonte única (gráfico e listinha da UI leem daqui).
    CORES: dict[str, str] = {
        Tipo.ROMPIMENTO.value: "#dc2626",   # vermelho
        Tipo.MANUTENCAO.value: "#2563eb",   # azul
        Tipo.QUEDA_LINK.value: "#f59e0b",   # âmbar
        Tipo.ATUALIZACAO.value: "#6b7280",  # cinza
        Tipo.OUTRO.value: "#7c3aed",        # roxo
    }

    tipo = models.CharField(
        max_length=16, choices=Tipo.choices, default=Tipo.OUTRO,
        help_text=_("Natureza do evento (define a cor no gráfico)."),
    )
    titulo = models.CharField(max_length=255)
    descricao = models.TextField(blank=True, default="")
    started_at = models.DateTimeField(help_text=_("Início do evento."))
    ended_at = models.DateTimeField(
        null=True, blank=True,
        help_text=_("Fim do evento; vazio = evento pontual."),
    )
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="eventos_rede",
    )

    history = HistoricalRecords()

    class Meta:
        verbose_name = _("Evento de rede")
        verbose_name_plural = _("Eventos de rede")
        ordering = ["-started_at"]
        indexes = [
            models.Index(fields=["organization", "started_at"]),
        ]

    def __str__(self) -> str:
        quando = self.started_at.isoformat() if self.started_at else "?"
        return f"{self.get_tipo_display()} — {self.titulo} ({quando})"

    @property
    def is_pontual(self) -> bool:
        """Evento sem fim registrado — vira linha tracejada, não faixa."""
        return self.ended_at is None

    @property
    def cor(self) -> str:
        return self.CORES.get(self.tipo, self.CORES[self.Tipo.OUTRO.value])
