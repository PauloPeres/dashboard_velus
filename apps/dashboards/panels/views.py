"""Views dos painéis de parede — a casca, servindo qualquer painel.

Nada aqui sabe o que é NOC ou executivo: tudo é `panel.key`. Quando o painel
executivo chegar, ele registra um `PanelSpec` e estas mesmas views passam a
servi-lo, com o mesmo pareamento, o mesmo cache e as mesmas regras de frescor —
que é justamente o que não pode divergir entre as duas telas.

**Quem pode ver um painel**, das duas formas:

- *logado*, pela aba correspondente (`panel.access_key`), como qualquer outra
  página do dashboard;
- *pela TV pareada*, com a credencial de display no cookie. A TV não tem usuário
  e nunca terá: ela vê o painel daquele painel, na organização que a aprovou, e
  nada mais.
"""

from __future__ import annotations

from typing import Any

import segno
import structlog
from django.contrib.auth.decorators import login_required
from django.core.cache import cache
from django.http import (
    Http404,
    HttpRequest,
    HttpResponse,
    HttpResponseForbidden,
    HttpResponseRedirect,
    JsonResponse,
)
from django.shortcuts import render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_POST

from apps.shared.context import set_current_organization
from apps.tenancy.models import DisplayDevice

from . import PanelSpec, all_panels, get_panel
from .pairing import (
    COOKIE_MAX_AGE,
    COOKIE_NOME,
    codigo_expirou,
    hash_segredo,
    novo_codigo,
    novo_segredo,
)

_logger = structlog.get_logger(__name__)


# =============================================================================
# Quem está pedindo: TV pareada ou pessoa logada
# =============================================================================
def _display_device(request: HttpRequest, panel: PanelSpec) -> DisplayDevice | None:
    """A TV pareada por trás desta requisição, se houver.

    Confere o painel: uma credencial emitida para o NOC não abre o executivo.
    Trocar de painel exige parear de novo, e é assim que se evita que uma TV da
    sala de operação amanheça mostrando faturamento na recepção.
    """
    token = request.COOKIES.get(COOKIE_NOME, "")
    if not token:
        return None
    device = DisplayDevice.objects.filter(
        display_token_hash=hash_segredo(token),
        panel_key=panel.key,
        revoked_at__isnull=True,
        approved_at__isnull=False,
    ).select_related("organization").first()
    return device


def _org_para_o_painel(request: HttpRequest, panel: PanelSpec) -> tuple[Any, DisplayDevice | None]:
    """Organização que este pedido pode ver, e a TV que o fez (ou None).

    Devolve `(None, None)` quando ninguém tem direito: nem TV pareada, nem
    pessoa logada com acesso à aba do painel.
    """
    device = _display_device(request, panel)
    if device is not None and device.organization_id:
        return device.organization, device

    user = getattr(request, "user", None)
    if user is None or not user.is_authenticated:
        return None, None
    membership = user.get_active_membership()
    if membership is None:
        return None, None
    permitidas = membership.allowed_page_keys()
    if "*" not in permitidas and panel.access_key not in permitidas:
        return None, None
    return membership.organization, None


# =============================================================================
# P1 — o snapshot, uma consulta por volta da rotação
# =============================================================================
def _snapshot_cacheado(panel: PanelSpec, org: Any, now: Any) -> dict[str, Any]:
    """Snapshot do painel, com cache curto compartilhado entre as TVs.

    Duas TVs na mesma sala, mais o navegador de quem está conferindo, não podem
    virar três vezes a carga. O cache é por painel **e** por organização — nunca
    global, ou uma TV veria o número da outra empresa.

    `gerado_em` vem de dentro do snapshot, não do relógio de quem lê: é isso que
    permite à tela dizer a idade real do dado mesmo servido do cache.
    """
    chave = f"panel:{panel.key}:{org.pk}"
    dados = cache.get(chave)
    if dados is None:
        set_current_organization(org)
        dados = panel.snapshot(org, now)
        cache.set(chave, dados, panel.cache_seconds)
    return dados


@never_cache
def panel_snapshot(request: HttpRequest, panel_key: str) -> JsonResponse:
    """JSON com tudo que os slides precisam (P1).

    Devolve **403 com corpo JSON** em vez de redirecionar para o login: quem
    chama isto é um `fetch` de uma TV, e um 302 para a tela de login viraria
    "painel congelado sem explicação" na parede.
    """
    panel = get_panel(panel_key)
    if panel is None:
        raise Http404("Painel não encontrado")

    org, device = _org_para_o_painel(request, panel)
    if org is None:
        return JsonResponse({"erro": "sem_acesso"}, status=403)

    now = timezone.now()
    dados = _snapshot_cacheado(panel, org, now)

    if device is not None:
        # `update` direto: o heartbeat da TV não precisa de histórico nem de
        # corrida com o resto do objeto.
        DisplayDevice.objects.filter(pk=device.pk).update(last_seen_at=now)

    barra = dados.get("barra", {})
    return JsonResponse(
        {
            "painel": panel.key,
            "gerado_em": dados.get("gerado_em", now).isoformat(),
            # A idade vai em segundos e é recalculada no cliente a cada tique:
            # carimbo estático o cérebro ignora, contador que anda, não (§2).
            "idade_segundos": barra.get("idade_segundos"),
            "idade_conhecida": barra.get("idade_conhecida", False),
            "dado_velho": barra.get("dado_velho", True),
            "barra": {
                "clientes_fora": barra.get("clientes_fora"),
                "massivas_abertas": barra.get("massivas_abertas"),
                "coleta_ok": barra.get("coleta_ok"),
                "coleta_mensagem": barra.get("coleta_mensagem", ""),
            },
        }
    )


# =============================================================================
# P0 — pareamento por QR
# =============================================================================
@never_cache
def panel_pair(request: HttpRequest, panel_key: str) -> HttpResponse:
    """Tela de pareamento: cria o pedido e mostra o QR + o código.

    Quem já tem credencial válida não passa por aqui — vai direto para o painel,
    que é o que uma TV religada depois de uma queda de energia precisa fazer
    sozinha.
    """
    panel = get_panel(panel_key)
    if panel is None:
        raise Http404("Painel não encontrado")

    if _display_device(request, panel) is not None:
        return HttpResponseRedirect(reverse("dashboards:panel", args=[panel.key]))

    device_token = novo_segredo()
    device = DisplayDevice.objects.create(
        panel_key=panel.key,
        code=_codigo_livre(),
        device_token_hash=hash_segredo(device_token),
    )

    url_aprovacao = request.build_absolute_uri(
        f"{reverse('dashboards:panel_approve')}?code={device.code}"
    )
    # QR gerado aqui dentro, sem serviço externo: mandar o código de pareamento
    # para uma API de QR de terceiro seria publicá-lo fora.
    qr_svg = segno.make(url_aprovacao, error="m").svg_inline(scale=6, dark="#0f172a")

    resposta = render(
        request,
        "dashboards/panels/pair.html",
        {
            "panel": panel,
            "codigo": device.code,
            "qr_svg": qr_svg,
            "url_aprovacao": url_aprovacao,
            "status_url": reverse("dashboards:panel_pair_status", args=[panel.key]),
        },
    )
    # O `device_token` vai só para o navegador da TV, nunca para a tela: é ele
    # que garante que a credencial seja entregue a quem pediu o pareamento, e
    # não a quem fotografou o QR.
    resposta.set_cookie(
        f"{COOKIE_NOME}_pair",
        device_token,
        max_age=60 * 30,
        httponly=True,
        samesite="Lax",
        secure=request.is_secure(),
    )
    return resposta


def _codigo_livre() -> str:
    """Código novo que não colida com um pareamento ainda vivo."""
    for _ in range(10):
        codigo = novo_codigo()
        if not DisplayDevice.objects.filter(code=codigo).exists():
            return codigo
    raise RuntimeError("não foi possível gerar código de pareamento único")


@never_cache
def panel_pair_status(request: HttpRequest, panel_key: str) -> JsonResponse:
    """A TV pergunta se já foi aprovada. Quando for, recebe a credencial.

    A credencial sai **uma vez**, aqui, para o navegador que tem o
    `device_token` — e é gravada como hash. Se esta resposta vazar, vazou para
    quem já era a TV.
    """
    panel = get_panel(panel_key)
    if panel is None:
        raise Http404("Painel não encontrado")

    token = request.COOKIES.get(f"{COOKIE_NOME}_pair", "")
    if not token:
        return JsonResponse({"estado": "sem_pedido"}, status=400)

    device = DisplayDevice.objects.filter(
        device_token_hash=hash_segredo(token), panel_key=panel.key
    ).first()
    if device is None:
        return JsonResponse({"estado": "sem_pedido"}, status=400)
    if device.revoked_at is not None:
        return JsonResponse({"estado": "revogado"})
    if device.approved_at is None:
        if codigo_expirou(device.created_at):
            return JsonResponse({"estado": "expirado"})
        return JsonResponse({"estado": "aguardando"})

    display_token = novo_segredo()
    DisplayDevice.objects.filter(pk=device.pk).update(
        display_token_hash=hash_segredo(display_token),
        # O código morre ao ser usado: ele já cumpriu o papel, e continuar
        # válido só daria a alguém uma segunda chance de aprovar a mesma TV.
        # NULL, e não string vazia, porque a coluna é única — e no Postgres
        # vários NULLs convivem.
        code=None,
    )
    resposta = JsonResponse(
        {"estado": "pareado", "painel": panel.key, "url": reverse("dashboards:panel", args=[panel.key])}
    )
    resposta.set_cookie(
        COOKIE_NOME,
        display_token,
        max_age=COOKIE_MAX_AGE,
        httponly=True,
        samesite="Lax",
        secure=request.is_secure(),
    )
    resposta.delete_cookie(f"{COOKIE_NOME}_pair")
    _logger.info("panel_display_paired", device=device.pk, panel=panel.key)
    return resposta


@login_required
@never_cache
def panel_approve(request: HttpRequest) -> HttpResponse:
    """Quem escaneou o QR aprova a TV — depois de fazer login normalmente.

    É aqui que o dispositivo ganha dono. Antes disto ele não pertence a
    organização nenhuma e não enxerga dado nenhum.
    """
    membership = request.user.get_active_membership()
    if membership is None:
        return HttpResponseForbidden("Sem organização ativa.")

    codigo = (request.GET.get("code") or request.POST.get("code") or "").strip().upper()
    device = (
        DisplayDevice.objects.filter(code=codigo, approved_at__isnull=True).first()
        if codigo
        else None
    )
    panel = get_panel(device.panel_key) if device else None

    # Sem código, esta página deixa de ser "aprovar" e vira **o começo do
    # pareamento**: até 21/09/2026 ela só existia como destino do QR, e quem
    # chegava por um link (ou pelo botão da home) via "código não encontrado",
    # que é um erro para quem não fez nada errado.
    sem_codigo = not codigo

    erro = ""
    if sem_codigo:
        erro = ""
    elif device is None:
        erro = "Código não encontrado, já usado ou expirado."
    elif codigo_expirou(device.created_at):
        erro = "Este código expirou. Recarregue a tela da TV para gerar outro."
    elif panel is None:
        erro = "Este código aponta para um painel que não existe mais."
    elif not _pode_no_painel(membership, panel):
        erro = "Você não tem acesso à aba que este painel mostra."

    if request.method == "POST" and not erro:
        nome = (request.POST.get("name") or "").strip()[:120]
        DisplayDevice.objects.filter(pk=device.pk).update(
            organization=membership.organization,
            approved_by=request.user,
            approved_at=timezone.now(),
            name=nome,
        )
        _logger.info(
            "panel_display_approved",
            device=device.pk,
            panel=device.panel_key,
            by=request.user.pk,
        )
        return render(
            request,
            "dashboards/panels/approve.html",
            {"aprovado": True, "panel": panel, "nome": nome},
        )

    return render(
        request,
        "dashboards/panels/approve.html",
        {
            "aprovado": False,
            "erro": erro,
            "codigo": codigo,
            "panel": panel,
            "sem_codigo": sem_codigo,
            # Os painéis que ESTA pessoa pode parear, com o endereço a digitar
            # na TV. Absoluto porque é para alguém teclar num controle remoto,
            # e um caminho relativo não serve num aparelho que não está aqui.
            "paineis": [
                {
                    "key": p.key,
                    "title": p.title,
                    "subtitle": p.subtitle,
                    "url": request.build_absolute_uri(
                        reverse("dashboards:panel", args=[p.key])
                    ),
                }
                for p in all_panels()
                if _pode_no_painel(membership, p)
            ]
            if sem_codigo
            else [],
        },
    )


def _pode_no_painel(membership: Any, panel: PanelSpec) -> bool:
    permitidas = membership.allowed_page_keys()
    return "*" in permitidas or panel.access_key in permitidas


@login_required
@never_cache
@require_POST
def panel_device_revoke(request: HttpRequest, device_id: int) -> HttpResponse:
    """Derruba uma TV pareada.

    Existe desde o primeiro dia, e não como melhoria futura: credencial que não
    se revoga é credencial eterna, e TV some, muda de sala e é roubada.
    """
    membership = request.user.get_active_membership()
    if membership is None or not membership.is_owner:
        return HttpResponseForbidden("Sem permissão para revogar dispositivos.")

    device = DisplayDevice.objects.filter(
        pk=device_id, organization=membership.organization
    ).first()
    if device is None:
        raise Http404("Dispositivo não encontrado")

    DisplayDevice.objects.filter(pk=device.pk).update(
        revoked_at=timezone.now(), display_token_hash=""
    )
    _logger.info("panel_display_revoked", device=device.pk, by=request.user.pk)
    return HttpResponseRedirect(f"{reverse('dashboards:settings')}?dispositivo_revogado=1")


# =============================================================================
# Ação pelo controle da TV — reconhecer e dizer a causa
# =============================================================================
@never_cache
@require_POST
def panel_massiva_acao(
    request: HttpRequest, panel_key: str, outage_id: int
) -> JsonResponse:
    """A TV marca "estou tratando" e, se souber, a causa (pedido de 21/09/2026).

    **Quem age aqui é o dispositivo, não uma pessoa** — o controle remoto não faz
    login. Por isso o autor gravado é o nome da TV ("marcado na TV da bancada")
    em vez de um usuário inventado: saber que foi alguém na sala já muda a ação
    de quem chega depois, e fingir autoria seria pior que não ter.

    Só a TV pareada pode fazer isto, e só na organização que a aprovou. Quem
    está logado usa a aba, que é onde o autor tem nome.
    """
    from apps.network.infrastructure.models import OutageEvent

    panel = get_panel(panel_key)
    if panel is None:
        raise Http404("Painel não encontrado")

    device = _display_device(request, panel)
    if device is None or not device.organization_id:
        return JsonResponse({"erro": "sem_acesso"}, status=403)

    # A TV não tem sessão, então o `TenantMiddleware` não pôs organização no
    # contexto: quem a define aqui é a credencial do aparelho. Sem isto, o
    # `TenantManager` recusa a consulta — e é assim que ele deve se comportar.
    set_current_organization(device.organization)

    outage = OutageEvent.objects.filter(
        organization=device.organization, pk=outage_id
    ).first()
    if outage is None:
        raise Http404("Massiva não encontrada")

    agora = timezone.now()
    campos: list[str] = []
    resposta: dict[str, Any] = {}

    if request.POST.get("acao") == "ciente" and outage.acknowledged_at is None:
        # Primeiro a assumir é quem fica — igual à aba. Sobrescrever apagaria
        # quem realmente pegou o evento.
        outage.acknowledged_at = agora
        outage.acknowledged_by_display = device.name or "TV"
        campos += ["acknowledged_at", "acknowledged_by_display"]
        resposta["ciente"] = outage.acknowledged_by_display

    causa = request.POST.get("causa", "")
    if causa:
        if causa not in OutageEvent.Cause.values:
            return JsonResponse({"erro": "causa_invalida"}, status=400)
        # A causa pode ser registrada com a massiva ainda aberta: quem está na
        # sala às vezes já sabe ("é rompimento") antes de o evento encerrar. A
        # fila da aba continua cobrando apenas as que encerraram sem resposta.
        outage.confirmed_cause = causa
        outage.cause_confirmed_at = agora
        outage.cause_note = (outage.cause_note or "") + f" [marcado na {device.name or 'TV'}]"
        campos += ["confirmed_cause", "cause_confirmed_at", "cause_note"]
        resposta["causa"] = outage.get_confirmed_cause_display()

    if not campos:
        return JsonResponse({"erro": "nada_a_fazer"}, status=400)

    outage.save(update_fields=[*campos, "updated_at"])
    _logger.info(
        "panel_massiva_acao",
        outage=outage.pk,
        device=device.pk,
        ciente=bool(resposta.get("ciente")),
        causa=causa or "",
    )
    return JsonResponse({"ok": True, **resposta})


# =============================================================================
# P2 — a casca do painel
# =============================================================================
@never_cache
def panel_view(request: HttpRequest, panel_key: str) -> HttpResponse:
    """O painel em si: casca escura, barra fixa e rotação de slides.

    Quem chega sem credencial e sem login vai para o pareamento — é o caminho de
    uma TV recém-ligada, e ela não tem como "clicar em entrar".
    """
    panel = get_panel(panel_key)
    if panel is None:
        raise Http404("Painel não encontrado")

    org, device = _org_para_o_painel(request, panel)
    if org is None:
        return HttpResponseRedirect(reverse("dashboards:panel_pair", args=[panel.key]))

    now = timezone.now()
    dados = _snapshot_cacheado(panel, org, now)

    from apps.dashboards import charts

    # Os gráficos são montados aqui, não no snapshot: o snapshot é dado, e o
    # mesmo dado serve a telas com temas diferentes (a aba é clara, a TV é
    # escura). Guardar a figura pronta no cache amarraria as duas.
    for linha in dados.get("massivas") or []:
        if linha.get("mapa"):
            linha["mapa_chart_json"] = charts.outage_map(linha["mapa"])

    if dados.get("mapa_dia", {}).get("pontos"):
        dados["mapa_dia"]["chart_json"] = charts.day_heat_map(dados["mapa_dia"])

    paginas = panel.paginas(dados)

    return render(
        request,
        "dashboards/panels/shell.html",
        {
            "panel": panel,
            # A TV não tem sessão e o contexto da organização não chega pelo
            # middleware: o logo e o nome vêm da credencial do aparelho.
            "organizacao": org,
            "paginas": paginas,
            "snapshot": dados,
            "device": device,
            "snapshot_url": reverse("dashboards:panel_snapshot", args=[panel.key]),
            "timeline_chart_json": (
                charts.outage_timeline(dados["timeline"]) if dados.get("timeline") else ""
            ),
        },
    )
