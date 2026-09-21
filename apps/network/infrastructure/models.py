"""Models de persistência do bounded context Network.

Herda `apps.shared.TenantModel` -> ganha `organization` FK indexada + TenantManager.
Identidade composta: `(organization, source_type, external_id)` é unique.
"""

from __future__ import annotations

from django.db import models
from django.utils.translation import gettext_lazy as _
from simple_history.models import HistoricalRecords

from apps.integrations.shared.enums import SourceType
from apps.shared.models import TenantModel


class Connection(TenantModel):
    """Estado de conexão de um cliente (RADIUS/PPPoE) vindo de fonte externa.

    `customer` é FK opcional pq sync pode receber conexões antes do cliente
    correspondente. Repository tenta resolver via
    `(organization, source_type, customer_external_id)` no upsert.
    """

    class Status(models.TextChoices):
        ONLINE = "ONLINE", _("Online")
        OFFLINE = "OFFLINE", _("Offline")
        BLOCKED = "BLOCKED", _("Bloqueado")
        UNKNOWN = "UNKNOWN", _("Desconhecido")

    source_type = models.CharField(
        max_length=32,
        choices=SourceType.choices,
        help_text=_("Sistema externo que originou este registro."),
    )
    external_id = models.CharField(
        max_length=128,
        help_text=_("ID da conexão no sistema externo (opaco — string)."),
    )

    # FK resolvida via (source_type, customer_external_id) no Repository.
    customer = models.ForeignKey(
        "customers.Customer",
        on_delete=models.PROTECT,
        related_name="connections",
        null=True,
        blank=True,
    )
    customer_external_id = models.CharField(max_length=128, db_index=True)
    contract_external_id = models.CharField(max_length=128, blank=True, default="")

    login = models.CharField(max_length=128, blank=True, default="")
    status = models.CharField(
        max_length=16, choices=Status.choices, default=Status.UNKNOWN
    )

    ip = models.CharField(max_length=64, blank=True, default="")
    nas_ip = models.CharField(max_length=64, blank=True, default="")
    rx_bytes = models.BigIntegerField(default=0)
    tx_bytes = models.BigIntegerField(default=0)
    download_speed = models.CharField(max_length=64, blank=True, default="")
    upload_speed = models.CharField(max_length=64, blank=True, default="")

    last_connection_at = models.DateTimeField(null=True, blank=True)

    # Topologia e queda promovidas de `raw_extras` em #143. Estavam no JSON
    # desde sempre e ninguém lia; viraram coluna quando o detector de massivas
    # passou a precisar filtrar e agrupar por elas — JSONField não sustenta
    # índice pra "todas as quedas desta CTO na última hora".
    cto_external_id = models.CharField(max_length=128, blank=True, default="")
    cto_port = models.CharField(max_length=32, blank=True, default="")
    pon_external_id = models.CharField(max_length=128, blank=True, default="")
    transmitter_external_id = models.CharField(max_length=128, blank=True, default="")
    concentrator_external_id = models.CharField(
        max_length=128, blank=True, default=""
    )
    latitude = models.FloatField(null=True, blank=True)
    longitude = models.FloatField(null=True, blank=True)
    disconnect_reason = models.CharField(max_length=64, blank=True, default="")
    last_disconnection_at = models.DateTimeField(null=True, blank=True)

    # --- sinal óptico da ONU (#148) ---
    # É com estes campos que a equipe pega **fusão mal feita depois de um
    # reparo**: o cliente volta a conectar, mas com 5 dB a menos do que tinha.
    #
    # `onu_external_id` é o id do registro em `radpop_radio_cliente_fibra` (o
    # `id_cliente_fibra`), e não uma duplicata do login: é a chave que o disparo
    # de medição exige. Sem ela, medir um cliente custaria uma listagem só para
    # descobrir qual ONU é a dele.
    onu_external_id = models.CharField(max_length=128, blank=True, default="")
    # Null (e não 0.0) porque **zero não é potência, é ausência de leitura**:
    # `sinal_rx = "0.00"` vem em 1.391 dos 4.554 registros do IXC, e ~30% das
    # ONUs simplesmente não reportam. Gravar 0.0 faria todo cliente caído
    # aparecer com ~24 dB de perda contra uma base típica de -24 dB.
    signal_rx = models.FloatField(
        null=True, blank=True, help_text=_("Potência de recepção (dBm). Nulo = sem leitura.")
    )
    signal_tx = models.FloatField(null=True, blank=True)
    # O carimbo é parte do dado, não metadado: a coleta passiva do IXC é diária
    # (~06:30), então uma leitura pode ter 18 horas — e a tela precisa dizer isso
    # em vez de apresentá-la como se fosse de agora.
    signal_measured_at = models.DateTimeField(null=True, blank=True)
    onu_run_state = models.CharField(max_length=32, blank=True, default="")
    # O que a OLT respondeu, literal (`dying-gasp`, `LOS`, `LOSi/LOBi`,
    # `reset`...). Vazio é **"a OLT não informou"**, não "sem causa" — e o código
    # não traduz: interpretar `dying-gasp` como falta de energia é leitura do
    # time, e ainda não foi observada numa massiva real.
    onu_last_drop_cause = models.CharField(max_length=64, blank=True, default="")
    onu_last_up_at = models.DateTimeField(null=True, blank=True)

    raw_extras = models.JSONField(default=dict, blank=True)

    history = HistoricalRecords()

    class Meta:
        verbose_name = _("Conexão")
        verbose_name_plural = _("Conexões")
        constraints = [
            models.UniqueConstraint(
                fields=["organization", "source_type", "external_id"],
                name="unique_connection_per_source",
            ),
        ]
        indexes = [
            models.Index(fields=["organization", "status"]),
            models.Index(fields=["organization", "last_connection_at"]),
            models.Index(fields=["organization", "nas_ip"]),
            models.Index(
                fields=["organization", "source_type", "customer_external_id"]
            ),
            # Acessos do detector: "quem está fora agrupado por caixa/porta" e
            # "quem caiu nesta janela".
            models.Index(fields=["organization", "cto_external_id"]),
            models.Index(fields=["organization", "pon_external_id"]),
            models.Index(fields=["organization", "last_disconnection_at"]),
        ]

    def __str__(self) -> str:
        return f"{self.login} [{self.status}] ({self.source_type}:{self.external_id})"


class BandwidthUsage(TenantModel):
    """Consumo de banda por cliente/período vindo de accounting RADIUS.

    `customer` é FK opcional pq sync pode receber consumo antes do cliente
    correspondente. Repository tenta resolver via
    `(organization, source_type, customer_external_id)` no upsert.
    """

    source_type = models.CharField(
        max_length=32,
        choices=SourceType.choices,
        help_text=_("Sistema externo que originou este registro."),
    )
    external_id = models.CharField(
        max_length=128,
        help_text=_("ID do registro de consumo no sistema externo (opaco — string)."),
    )

    # FK resolvida via (source_type, customer_external_id) no Repository.
    customer = models.ForeignKey(
        "customers.Customer",
        on_delete=models.PROTECT,
        related_name="bandwidth_usages",
        null=True,
        blank=True,
    )
    customer_external_id = models.CharField(max_length=128, db_index=True)

    download_bytes = models.BigIntegerField(default=0)
    upload_bytes = models.BigIntegerField(default=0)
    session_time = models.BigIntegerField(default=0)  # segundos

    reference_date = models.DateField(null=True, blank=True)

    raw_extras = models.JSONField(default=dict, blank=True)

    history = HistoricalRecords()

    class Meta:
        verbose_name = _("Consumo de banda")
        verbose_name_plural = _("Consumos de banda")
        constraints = [
            models.UniqueConstraint(
                fields=["organization", "source_type", "external_id"],
                name="unique_bandwidth_usage_per_source",
            ),
        ]
        indexes = [
            models.Index(fields=["organization", "reference_date"]),
            models.Index(
                fields=["organization", "source_type", "customer_external_id"]
            ),
        ]

    def __str__(self) -> str:
        return (
            f"{self.customer_external_id} "
            f"↓{self.download_bytes} ↑{self.upload_bytes} "
            f"({self.source_type}:{self.external_id})"
        )


class NetworkElement(TenantModel):
    """Elemento da planta de rede — CTO, POP, porta PON, OLT ou cabo (#142).

    Existe pra que a agregação de CTOs e o detector de massivas leiam topologia
    do banco em vez de bater no IXC no meio do render (que era o que
    `_fetch_cto_catalog` fazia: uma chamada HTTP por pageview, e domínio
    importando `apps.integrations` — AGENT.md §1.6).

    A hierarquia é guardada como par `(parent_kind, parent_external_id)` e não
    como FK: os elementos chegam do sync em ordem arbitrária e uma FK obrigaria
    a ordenar a carga por nível. Quem precisa navegar resolve com um dict em
    memória — a planta inteira tem milhares de linhas, não milhões.
    """

    class Kind(models.TextChoices):
        CTO = "CTO", _("Caixa de atendimento (CTO)")
        POP = "POP", _("Ponto de presença (POP)")
        PON = "PON", _("Porta PON")
        OLT = "OLT", _("Transmissor (OLT)")
        CABLE = "CABLE", _("Cabo")
        # A caixa de emenda é onde o cabo é aberto e refeito — o lugar que o
        # técnico abre primeiro quando o trecho suspeito passa por ali. Vem do
        # InMap (`df_elemento` tipo CA), com posição própria, e por isso entra
        # como elemento de planta e não como detalhe do cabo.
        SPLICE = "SPLICE", _("Caixa de emenda")

    source_type = models.CharField(
        max_length=32,
        choices=SourceType.choices,
        help_text=_("Sistema externo que originou este registro."),
    )
    kind = models.CharField(max_length=16, choices=Kind.choices)
    external_id = models.CharField(
        max_length=128,
        help_text=_("ID do elemento no sistema externo (opaco — string)."),
    )

    name = models.CharField(max_length=255, blank=True, default="")

    # Null (e não 0.0) quando a origem não tem posição: cabo nunca tem, e uma
    # coordenada (0, 0) cairia no golfo da Guiné e contaminaria cluster geográfico.
    latitude = models.FloatField(null=True, blank=True)
    longitude = models.FloatField(null=True, blank=True)

    parent_external_id = models.CharField(max_length=128, blank=True, default="")
    parent_kind = models.CharField(
        max_length=16, choices=Kind.choices, blank=True, default=""
    )

    capacity = models.IntegerField(null=True, blank=True)
    address = models.CharField(max_length=255, blank=True, default="")
    project_external_id = models.CharField(max_length=128, blank=True, default="")
    status = models.CharField(max_length=32, blank=True, default="")

    raw_extras = models.JSONField(default=dict, blank=True)

    class Meta:
        verbose_name = _("Elemento de rede")
        verbose_name_plural = _("Elementos de rede")
        constraints = [
            models.UniqueConstraint(
                fields=["organization", "source_type", "kind", "external_id"],
                name="unique_network_element_per_source",
            ),
        ]
        indexes = [
            # O detector varre por tipo ("todas as CTOs") e sobe a hierarquia
            # ("todas as PONs desta OLT") — são os dois acessos quentes.
            models.Index(fields=["organization", "kind"]),
            models.Index(fields=["organization", "kind", "parent_external_id"]),
            models.Index(
                fields=["organization", "source_type", "kind", "external_id"],
                name="netelem_lookup_idx",
            ),
        ]

    def __str__(self) -> str:
        return f"{self.get_kind_display()} {self.name or self.external_id}"

    @property
    def has_position(self) -> bool:
        return self.latitude is not None and self.longitude is not None


class NetworkElementGeometry(TenantModel):
    """O traçado do elemento — a polilinha que o InMap desenha (épico da geometria).

    Separado de `NetworkElement` porque é outro ciclo de vida e outro tamanho: o
    elemento é uma linha com nome e pai, a geometria é uma lista de até 121
    pontos que só existe para 1.191 cabos e algumas centenas de caixas de
    emenda. Juntar os dois faria toda query de planta carregar polilinha.

    **Os pontos ficam em JSON, não em tabela de vértice.** São 12.922 vínculos na
    origem, mas ninguém pergunta por vértice: a geometria é lida inteira, para
    desenhar ou para medir distância. Uma tabela de vértice seria 12.922 linhas
    para responder exatamente as mesmas perguntas que 1.191 documentos.

    Formato de `points`: `[[lat, lon], [lat, lon], ...]`, **na ordem do traçado**
    (o `sequencia` da origem). A ordem é o dado — invertê-la ou embaralhá-la
    transforma o cabo em zigue-zague.

    Isto é **cadastro, não medição**: a linha diz por onde o projeto passa o
    cabo, não por onde a fibra está enterrada hoje, e menos ainda onde ela
    rompeu.
    """

    source_type = models.CharField(
        max_length=32,
        choices=SourceType.choices,
        help_text=_("Sistema externo que originou este traçado."),
    )
    kind = models.CharField(max_length=16, choices=NetworkElement.Kind.choices)
    external_id = models.CharField(
        max_length=128,
        help_text=_("ID do elemento na origem — o mesmo de NetworkElement."),
    )

    name = models.CharField(max_length=255, blank=True, default="")
    project_external_id = models.CharField(max_length=128, blank=True, default="")
    type_name = models.CharField(
        max_length=255,
        blank=True,
        default="",
        help_text=_(
            "Nome do tipo na origem — é ele que diz a classe do cabo, e não a "
            "descrição do elemento."
        ),
    )

    points = models.JSONField(
        default=list,
        blank=True,
        help_text=_("[[lat, lon], ...] na ordem do traçado."),
    )

    class Meta:
        verbose_name = _("Traçado de elemento")
        verbose_name_plural = _("Traçados de elementos")
        constraints = [
            models.UniqueConstraint(
                fields=["organization", "source_type", "kind", "external_id"],
                name="unique_element_geometry_per_source",
            ),
        ]
        indexes = [
            # O acesso é sempre "todos os traçados deste tipo" — o candidato a
            # cabo é escolhido por distância, em memória, sobre o conjunto todo.
            models.Index(fields=["organization", "kind"]),
        ]

    def __str__(self) -> str:
        return f"traçado {self.kind} {self.name or self.external_id} ({len(self.points)} pontos)"

    @property
    def is_line(self) -> bool:
        """Dois pontos são o mínimo para existir traçado; um ponto é posição."""
        return len(self.points) >= 2


class ConnectionDropEvent(TenantModel):
    """Uma queda de login (#143) — **estado de trabalho, não arquivo**.

    Existe pra responder, agora: quem está fora e quem já voltou. É append-only
    no sentido de que nenhuma linha é reescrita para trás, mas não é série
    histórica: a durabilidade da massiva mora no `OutageEvent` (#145), que
    guarda quando, quantos e onde de forma genérica. Aqui não entra campo nem
    índice que só sirva pra analytics do passado.

    Existe como tabela própria porque `HistoricalConnection` guarda o estado
    corrente versionado, não as transições — nota já conhecida do projeto (#20).

    Os campos de topologia são **snapshot do instante da queda**, não FK pro
    estado atual: o cliente pode mudar de CTO ou de PON depois, e a massiva de
    ontem tem que continuar contando o que era ontem.

    `restored_at` nulo significa "ainda fora" — é o que o poll fecha quando o
    login volta a online.
    """

    connection = models.ForeignKey(
        "network.Connection",
        on_delete=models.CASCADE,
        related_name="drop_events",
    )
    # FK opcional: a conexão pode ter caído antes de o cliente ter sido
    # sincronizado, e perder a queda por causa disso seria pior que não ter o nome.
    customer = models.ForeignKey(
        "customers.Customer",
        on_delete=models.SET_NULL,
        related_name="connection_drop_events",
        null=True,
        blank=True,
    )

    login = models.CharField(max_length=128, blank=True, default="")

    dropped_at = models.DateTimeField()
    restored_at = models.DateTimeField(null=True, blank=True)
    reason = models.CharField(max_length=64, blank=True, default="")

    # --- snapshot da topologia no instante da queda ---
    cto_external_id = models.CharField(max_length=128, blank=True, default="")
    cto_port = models.CharField(max_length=32, blank=True, default="")
    pon_external_id = models.CharField(max_length=128, blank=True, default="")
    transmitter_external_id = models.CharField(max_length=128, blank=True, default="")
    latitude = models.FloatField(null=True, blank=True)
    longitude = models.FloatField(null=True, blank=True)
    monthly_amount = models.DecimalField(
        max_digits=12,
        decimal_places=2,
        null=True,
        blank=True,
        help_text=_("MRR do contrato no momento da queda — base do MRR afetado."),
    )

    # --- sinal antes e depois (#148) ---
    # O par que responde à pergunta do time: "com que sinal ele caiu e com que
    # sinal voltou?". Uma piora além do limiar depois de um reparo é o assinante
    # de fusão mal feita — a base é estável o bastante para isso significar algo
    # (uma ONU saudável variou menos de 0,7 dB em dez dias).
    #
    # `signal_rx_before` é a última leitura **válida** anterior à queda, buscada
    # no histórico quando a corrente está zerada. Zero jamais entra aqui: seria
    # ausência de leitura disfarçada de valor, e produziria uma perda fictícia de
    # ~24 dB em cada cliente caído.
    signal_rx_before = models.FloatField(null=True, blank=True)
    # Carimbo separado porque a distância entre os dois momentos é informação:
    # comparar uma base de 18 horas atrás com uma medição de agora é legítimo,
    # mas a tela tem que declarar isso.
    signal_before_measured_at = models.DateTimeField(null=True, blank=True)
    # Medido **no retorno**, pela consulta ativa à OLT. Nulo é o caso comum e
    # honesto: ~30% das ONUs não reportam sinal, e há ONU que a OLT não conhece
    # mais.
    signal_rx_after = models.FloatField(null=True, blank=True)
    signal_after_measured_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        verbose_name = _("Evento de queda")
        verbose_name_plural = _("Eventos de queda")
        constraints = [
            # Uma queda aberta por conexão. Sem isso, dois polls concorrentes
            # (ou um retry do Celery) abririam a mesma queda duas vezes e
            # dobrariam o tamanho da massiva.
            models.UniqueConstraint(
                fields=["organization", "connection"],
                condition=models.Q(restored_at__isnull=True),
                name="unique_open_drop_per_connection",
            ),
        ]
        indexes = [
            # Os dois acessos de tempo real: "quem caiu nesta janela" e "quem
            # ainda está fora". Nada de índice pra recorte histórico — a
            # durabilidade da massiva mora no OutageEvent, não aqui.
            models.Index(fields=["organization", "dropped_at"]),
            models.Index(fields=["organization", "restored_at"]),
        ]

    def __str__(self) -> str:
        estado = "aberta" if self.restored_at is None else "encerrada"
        return f"queda {self.login} @ {self.dropped_at:%d/%m %H:%M} ({estado})"

    @property
    def is_open(self) -> bool:
        return self.restored_at is None

    @property
    def signal_delta_db(self) -> float | None:
        """Quanto o sinal piorou (negativo) ou melhorou (positivo), em dB.

        `None` quando falta uma das pontas — que é o caso frequente, não a
        exceção. Nulo aqui significa "não dá para comparar", e a tela declara a
        cobertura em vez de fingir uma lista completa.

        Quem decide se o número é grande é quem chama, com o limiar configurável
        (`apps.network.application.optical_signal.degradation_threshold_db`): 3 dB
        é sugestão calibrada na base real, não constante mágica de domínio.
        """
        if self.signal_rx_before is None or self.signal_rx_after is None:
            return None
        return self.signal_rx_after - self.signal_rx_before


class ConnectionPollState(TenantModel):
    """Desde quando observamos o estado de conexão desta organização (#144).

    Uma linha por organização. Existe por causa da **partida a frio** (§5.7 do
    plano): o IXC tem hoje 237 logins ativos offline, boa parte caída há
    semanas. Sem saber o instante em que passamos a olhar, o diff de quedas só
    pode confiar na transição observada online → offline — e um login que já
    estava offline no banco, voltou e caiu de novo entre dois polls jamais
    abriria evento, porque o poll só lista quem está fora e nunca o vê online.

    `last_poll_at` não é métrica de vaidade: a tela é de tempo real e precisa
    dizer de quando é a foto que está mostrando.
    """

    baseline_at = models.DateTimeField(
        help_text=_("Instante da primeira leitura de status — antes disso, nada é queda."),
    )
    last_poll_at = models.DateTimeField(null=True, blank=True)
    last_success_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        verbose_name = _("Estado do poll de conexões")
        verbose_name_plural = _("Estados do poll de conexões")
        constraints = [
            models.UniqueConstraint(
                fields=["organization"], name="unique_connection_poll_state_per_org"
            ),
        ]

    def __str__(self) -> str:
        return f"poll desde {self.baseline_at:%d/%m %H:%M}"


class OutageEvent(TenantModel):
    """Uma massiva detectada (#145) — **este é o registro que dura**.

    O `ConnectionDropEvent` é estado de trabalho e responde "quem está fora
    agora"; aqui mora o agregado que sobrevive ao fim do evento: quando começou,
    quantos clientes, onde, qual elemento/trecho ficou sob suspeita.

    Os campos de escopo são **da última detecção**, não do primeiro instante: o
    detector roda a cada 3 min sobre as quedas ainda abertas, e uma massiva que
    começa numa caixa e escala pra OLT continua sendo a mesma ocorrência — o que
    muda é o que sabemos dela. Ver `apps.network.application.outage_detection`
    para a regra de identidade.

    `element_label` e `affected_fraction` são guardados separados de propósito:
    escopo alto com fração baixa significa "algo abaixo desta OLT", nunca "esta
    OLT caiu" (§2.6). Quem costura a frase é o template.
    """

    class Scope(models.TextChoices):
        CTO = "CTO", _("Caixa (CTO)")
        PON = "PON", _("Porta PON")
        OLT = "OLT", _("Transmissor (OLT)")
        POP = "POP", _("POP")
        GEO = "GEO", _("Proximidade geográfica")

    class Confidence(models.TextChoices):
        HIGH = "ALTA", _("Alta")
        MEDIUM = "MEDIA", _("Média")
        LOW = "BAIXA", _("Baixa")

    started_at = models.DateTimeField()
    ended_at = models.DateTimeField(
        null=True,
        blank=True,
        help_text=_("Preenchido quando ≥90% dos afetados voltou (§6)."),
    )
    last_detected_at = models.DateTimeField(
        help_text=_("Última rodada do poll em que o detector ainda viu esta massiva."),
    )

    scope = models.CharField(max_length=8, choices=Scope.choices)
    element_external_id = models.CharField(max_length=128, blank=True, default="")
    element_label = models.CharField(max_length=255, blank=True, default="")

    # FK opcional: o elemento pode não estar cadastrado (a topologia da queda vem
    # do snapshot do login, que existe antes do sync de planta), e o escopo GEO
    # não tem elemento nenhum.
    suspected_element = models.ForeignKey(
        "network.NetworkElement",
        on_delete=models.SET_NULL,
        related_name="outage_events",
        null=True,
        blank=True,
    )
    suspected_segment_label = models.CharField(
        max_length=255,
        blank=True,
        default="",
        help_text=_("Trecho suspeito entre caixas — nunca 'cabo X', que a API não permite afirmar (§2.3)."),
    )

    confidence = models.CharField(max_length=8, choices=Confidence.choices)

    # -- Causa confirmada (R9) ------------------------------------------------
    # A primeira entrada de dados desta ferramenta. O veredito automático (R7)
    # adivinha energia ou fibra e nunca fica sabendo se acertou; é este campo,
    # preenchido por gente, que fecha o ciclo — e que vira rótulo de modelo
    # quando houver passado suficiente.
    #
    # Fica no próprio evento, e não em tabela à parte, porque é 1:1 com ele e
    # porque a pergunta "quais massivas ainda não têm causa" precisa ser um
    # filtro, não um join.
    class Cause(models.TextChoices):
        ROMPIMENTO = "ROMPIMENTO", _("Rompimento de fibra")
        ENERGIA = "ENERGIA", _("Falta de energia")
        EQUIPAMENTO = "EQUIPAMENTO", _("Equipamento (OLT)")
        MANUTENCAO = "MANUTENCAO", _("Manutenção programada")
        FALSO_POSITIVO = "FALSO_POSITIVO", _("Falso positivo")

    confirmed_cause = models.CharField(
        max_length=20,
        choices=Cause.choices,
        blank=True,
        default="",
        help_text=_("O que a massiva era de verdade — preenchido por pessoa, nunca pelo sistema."),
    )
    # Tags são o contexto que a causa exclusiva não carrega ("rompimento" +
    # "vandalismo" + "troca de poste"). Ficam fora da causa de propósito: se
    # tudo fosse tag, nada seria a resposta e o modelo não teria o que prever.
    cause_tags = models.JSONField(default=list, blank=True)
    cause_note = models.TextField(blank=True, default="")
    cause_confirmed_by = models.ForeignKey(
        "tenancy.User",
        on_delete=models.SET_NULL,
        related_name="outage_causes_confirmed",
        null=True,
        blank=True,
        help_text=_("Quem preencheu. Rótulo sem autor não se audita."),
    )
    cause_confirmed_at = models.DateTimeField(null=True, blank=True)
    # Massiva descartada da fila de causa **de propósito**. Não é o mesmo que
    # "sem causa": é "ninguém vai lembrar o que foi isso, e chute vira rótulo
    # errado no treino".
    #
    # Guardar a diferença importa no dia em que o modelo for treinado: sem este
    # campo, a série não distingue o que ficou por responder do que foi
    # deliberadamente descartado — e a segunda categoria não é ruído, é decisão.
    cause_waived_at = models.DateTimeField(null=True, blank=True)
    cause_waived_reason = models.CharField(max_length=255, blank=True, default="")

    # -- Reconhecimento (P8 do painel de TV) ----------------------------------
    # "Ciente, o Fulano está tratando". Converte o painel de gritador em
    # coordenador: quem chega na sala vê que alguém já pegou o evento, em vez de
    # ligar para o mesmo técnico pela terceira vez.
    #
    # É diferente da causa confirmada (R9): esta é uma afirmação sobre AGORA
    # ("estou tratando"), aquela é sobre o passado ("era rompimento"). Guardar as
    # duas no mesmo campo perderia a única coisa que o reconhecimento mede — o
    # tempo entre o evento aparecer e alguém assumir.
    acknowledged_by = models.ForeignKey(
        "tenancy.User",
        on_delete=models.SET_NULL,
        related_name="outages_acknowledged",
        null=True,
        blank=True,
    )
    acknowledged_at = models.DateTimeField(null=True, blank=True)
    # Quando o reconhecimento vem da TV, não há usuário: o controle remoto não
    # faz login. Guarda-se o nome do dispositivo, e a tela diz "marcado na TV da
    # bancada" em vez de inventar um autor. Saber que foi alguém na sala já
    # muda a ação de quem chega depois.
    acknowledged_by_display = models.CharField(max_length=120, blank=True, default="")
    # Carimbo do push que saiu (P7). Um evento, um escalonamento: massiva que
    # cresce não manda mensagem de novo. Sem este campo, o loop de 5 min viraria
    # uma mensagem a cada 5 min — que é como se desliga um canal de alerta.
    escalated_at = models.DateTimeField(null=True, blank=True)

    # -- Conclusão manual (21/09/2026) ----------------------------------------
    # A massiva encerra sozinha quando ≥90% voltou, ou por inanição depois de
    # horas sem detecção. Faltava o caso em que a operação SABE que acabou antes
    # do número: o poste foi trocado, os que sobraram são ONU queimada, e o
    # evento fica aberto na tela chamando atenção para um reparo já feito.
    #
    # Campo próprio, e não só um `ended_at` preenchido, porque a diferença
    # importa depois: uma massiva encerrada por 90% de retorno e uma encerrada
    # por decisão de gente não medem a mesma coisa, e misturá-las estragaria a
    # estatística de duração — que é o insumo do "tempo típico de reparo".
    closed_manually_at = models.DateTimeField(null=True, blank=True)
    closed_manually_by = models.ForeignKey(
        "tenancy.User",
        on_delete=models.SET_NULL,
        related_name="outages_closed",
        null=True,
        blank=True,
    )
    closed_manual_reason = models.CharField(max_length=255, blank=True, default="")

    # -- Manutenção programada (R10) ------------------------------------------
    # A janela de manutenção é o **evento de rede** que a equipe já cadastra na
    # aba de Tendências (`atendimento.EventoRede` com tipo MANUTENCAO). Um
    # cadastro só: dois cadastros paralelos garantiriam o dia em que alguém
    # avisa num lugar e a massiva alarma do outro.
    #
    # Guardado como id solto, e não como FK: o vínculo atravessa bounded
    # contexts, e o network não importa models do atendimento (AGENT.md §1.1) —
    # ele pergunta por um serviço de aplicação. O rótulo vem junto para a tela
    # não depender da outra base para dizer qual manutenção era.
    maintenance_event_id = models.PositiveIntegerField(
        null=True,
        blank=True,
        help_text=_("EventoRede que explica esta massiva — gravado no nascimento dela."),
    )
    maintenance_label = models.CharField(max_length=255, blank=True, default="")

    affected_count = models.PositiveIntegerField(default=0)
    restored_count = models.PositiveIntegerField(default=0)
    affected_fraction = models.FloatField(
        default=0.0,
        help_text=_("Afetados / logins ativos do elemento em escopo."),
    )
    mrr_at_risk = models.DecimalField(max_digits=12, decimal_places=2, default=0)

    class Meta:
        verbose_name = _("Massiva")
        verbose_name_plural = _("Massivas")
        indexes = [
            # O acesso de tempo real: "quais massivas estão abertas agora".
            # O histórico é curto (poucas por dia) e ordena sem índice próprio —
            # índice pra recorte do passado seria contra a §1 do plano.
            models.Index(fields=["organization", "ended_at"]),
        ]

    def __str__(self) -> str:
        estado = "aberta" if self.ended_at is None else "encerrada"
        return f"massiva {self.scope} {self.element_label} ({self.affected_count} clientes, {estado})"

    @property
    def is_open(self) -> bool:
        return self.ended_at is None

    @property
    def restored_fraction(self) -> float:
        return (self.restored_count / self.affected_count) if self.affected_count else 0.0

    @property
    def has_confirmed_cause(self) -> bool:
        return bool(self.confirmed_cause)

    @property
    def is_acknowledged(self) -> bool:
        return self.acknowledged_at is not None

    @property
    def cause_waived(self) -> bool:
        return self.cause_waived_at is not None

    @property
    def closed_manually(self) -> bool:
        """Encerrada por decisão de gente, não por contagem de retorno.

        A tela diz isso em voz alta: sem a marca, a duração do evento parece
        medida quando foi declarada.
        """
        return self.closed_manually_at is not None

    @property
    def is_expected(self) -> bool:
        """Nasceu dentro de uma janela de manutenção programada.

        Esperada sai do alarme, não da tela: continua sendo registro do que
        aconteceu com os clientes, e escondê-la faria a estatística de
        disponibilidade mentir para melhor.
        """
        return self.maintenance_event_id is not None


class OutageAffectedLogin(TenantModel):
    """Liga a massiva a uma queda individual (#145/#147).

    `login`, `dropped_at` e `restored_at` são copiados do `ConnectionDropEvent`
    em vez de lidos por FK: a queda é estado de trabalho e pode ser podada, e a
    massiva tem que continuar dizendo quantos caíram e quando voltaram. Por isso
    a FK é `SET_NULL` — perder a queda não pode apagar o agregado.
    """

    outage = models.ForeignKey(
        "network.OutageEvent",
        on_delete=models.CASCADE,
        related_name="affected_logins",
    )
    drop_event = models.ForeignKey(
        "network.ConnectionDropEvent",
        on_delete=models.SET_NULL,
        related_name="outage_links",
        null=True,
        blank=True,
    )

    login = models.CharField(max_length=128, blank=True, default="")
    dropped_at = models.DateTimeField()
    restored_at = models.DateTimeField(null=True, blank=True)
    monthly_amount = models.DecimalField(
        max_digits=12, decimal_places=2, null=True, blank=True
    )

    class Meta:
        verbose_name = _("Cliente afetado por massiva")
        verbose_name_plural = _("Clientes afetados por massiva")
        constraints = [
            # Uma queda pertence a no máximo uma massiva. É o que impede que a
            # redetecção a cada 3 min duplique o afetado dentro do mesmo evento.
            models.UniqueConstraint(
                fields=["outage", "drop_event"],
                name="unique_affected_drop_per_outage",
            ),
        ]
        indexes = [
            models.Index(fields=["organization", "outage"]),
        ]

    def __str__(self) -> str:
        return f"{self.login} em {self.outage_id}"
