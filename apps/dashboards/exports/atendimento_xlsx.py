"""Planilha de atendimentos com link de WhatsApp — o export que substitui o CSV.

Pedido de 21/09/2026. O CSV entregava os números, mas quem ia usar a lista para
falar com as pessoas tinha de montar cada link na mão. A planilha entrega o link
pronto — e, principalmente, **um link que se refaz quando a mensagem muda**.

Por isso é fórmula, não texto. A aba *Configuração* tem a mensagem padrão; a
coluna WhatsApp aponta para ela. Mudou a mensagem numa célula, os 300 links
mudam junto. Se fossem links prontos, cada ajuste de texto exigiria exportar de
novo — e é assim que uma planilha vira desatualizada em silêncio.

**Duas colunas de telefone, sempre.** O número de quem falou (do Opa, que tem
WhatsApp por definição) e o do cadastro (do IXC). *Medido em produção:* eles
divergem em quase 30% dos casos, e o link usa o primeiro — com o do cadastro, um
em cada três contatos iria para um número que a pessoa talvez não use mais.
"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import datetime
from typing import Any

from openpyxl import Workbook
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter

# Onde a mensagem padrão mora. A fórmula da coluna WhatsApp aponta para cá, e é
# esta célula que a pessoa edita para mudar todos os links de uma vez.
CELULA_MENSAGEM = "Configuração!$B$2"

MENSAGEM_PADRAO = (
    "Olá {nome}, aqui é da Velus. Estou retornando sobre seu atendimento."
)

CABECALHO = (
    "Cliente",
    "Documento",
    "Telefone (conversa)",
    "Telefone (cadastro)",
    "Horário",
    "Atendente",
    "Departamento",
    "Categorias",
    "Protocolo",
    "Status",
    "WhatsApp",
)

_LARGURAS = (30, 16, 20, 20, 14, 20, 20, 34, 14, 14, 18)

_AZUL = "1D4ED8"
_CINZA = "F3F4F6"


def _cabecalho_formatado(ws: Any) -> None:
    ws.append(list(CABECALHO))
    for i, largura in enumerate(_LARGURAS, start=1):
        ws.column_dimensions[get_column_letter(i)].width = largura
        celula = ws.cell(row=1, column=i)
        celula.font = Font(bold=True, color="FFFFFF")
        celula.fill = PatternFill("solid", fgColor=_AZUL)
        celula.alignment = Alignment(vertical="center")
    ws.freeze_panes = "A2"


def _formula_whatsapp(linha: int) -> str:
    """Link do WhatsApp com a mensagem da aba de configuração.

    Três cuidados que a fórmula carrega:

    - **`{nome}` é substituído pelo nome da linha**, então a mensagem sai
      pessoal sem ninguém editar 300 células;
    - **o DDI 55 entra aqui**, porque a coluna mostra o telefone como a pessoa o
      lê ("(15) 99172-9534") e o `wa.me` exige o número internacional;
    - **`ENCODEURL` codifica o texto** para a URL. Ele existe no Excel e no
      LibreOffice; onde não existir, a planilha mostra `#NAME?` na célula em vez
      de gerar um link quebrado que abriria uma conversa com lixo no texto —
      erro visível é melhor que link silenciosamente errado;
    - **sem número, sem link.** A célula fica vazia em vez de virar um `wa.me/`
      que abre o WhatsApp sem destinatário.
    """
    numero = f"C{linha}"
    nome = f"A{linha}"
    mensagem = f'SUBSTITUTE({CELULA_MENSAGEM},"{{nome}}",{nome})'
    # O telefone sai formatado — "(15) 99172-9534" — e o wa.me só aceita
    # dígitos. Faltava o HÍFEN nesta limpeza, e a verificação em produção pegou:
    # o link saía como wa.me/1599172-9534 e abriria uma conversa inexistente.
    limpo = numero
    for char in (" ", "(", ")", "-"):
        limpo = f'SUBSTITUTE({limpo},"{char}","")'
    return (
        f'=IF({numero}="","",'
        f'HYPERLINK("https://wa.me/55"&{limpo}'
        f'&"?text="&ENCODEURL({mensagem}),"Abrir conversa"))'
    )


def _aba_configuracao(
    wb: Workbook,
    *,
    exportado_em: datetime,
    recorte: str,
    total: int,
    com_numero: int,
    mensagem: str,
) -> None:
    """A aba que explica o arquivo e controla os links.

    Ela existe por dois motivos. O primeiro é operacional: é onde se escreve a
    mensagem que vai para todo mundo. O segundo é de honestidade — uma planilha
    que sai do sistema perde o contexto no primeiro encaminhamento de e-mail, e
    sem a data e o recorte escritos dentro dela ninguém sabe se está olhando os
    atendimentos de ontem ou os de três meses atrás.
    """
    ws = wb.create_sheet("Configuração", 0)
    ws.column_dimensions["A"].width = 26
    ws.column_dimensions["B"].width = 80

    linhas = [
        ("Mensagem padrão", mensagem),
        ("", "Use {nome} onde o nome do cliente deve entrar."),
        ("", "Editar a célula acima atualiza o link de TODAS as linhas."),
        ("", ""),
        ("Exportado em", exportado_em.strftime("%d/%m/%Y %H:%M")),
        ("Recorte", recorte),
        ("Atendimentos", total),
        ("Com número para WhatsApp", com_numero),
        ("", ""),
        (
            "Sobre os telefones",
            "São dois: o da CONVERSA (número de quem falou, sempre com WhatsApp) "
            "e o do CADASTRO (IXC). Em cerca de 30% dos casos eles divergem — o "
            "link usa o da conversa, que é onde a pessoa está.",
        ),
    ]
    # A primeira linha é a mensagem; as duas seguintes são instrução, e por isso
    # ficam ao lado dela e não numa legenda de rodapé que ninguém lê.
    ws.append(["Configuração da exportação", ""])
    ws["A1"].font = Font(bold=True, size=13)
    for rotulo, valor in linhas:
        ws.append([rotulo, valor])

    ws["A2"].font = Font(bold=True)
    ws["B2"].fill = PatternFill("solid", fgColor="FEF3C7")
    ws["B2"].alignment = Alignment(wrap_text=True, vertical="top")
    ws.row_dimensions[2].height = 34
    ws["B11"].alignment = Alignment(wrap_text=True, vertical="top")
    ws.row_dimensions[11].height = 58
    for linha in (3, 4):
        ws[f"B{linha}"].font = Font(italic=True, color="6B7280")
    for linha in (6, 7, 8, 9):
        ws[f"A{linha}"].fill = PatternFill("solid", fgColor=_CINZA)


def montar_planilha(
    rows: Iterable[dict[str, Any]],
    *,
    exportado_em: datetime,
    recorte: str,
    mensagem: str = MENSAGEM_PADRAO,
) -> Workbook:
    """Monta o workbook: aba de configuração + aba de dados com os links."""
    wb = Workbook()
    ws = wb.active
    ws.title = "Atendimentos"
    _cabecalho_formatado(ws)

    total = 0
    com_numero = 0
    for r in rows:
        total += 1
        if r.get("telefone_conversa"):
            com_numero += 1
        ws.append([
            # Nome cru, sem o traço da tela: esta coluna alimenta a mensagem do
            # WhatsApp, e "Olá —" é pior que "Olá".
            r.get("customer_name_raw", r["customer_name"]),
            r["customer_document"],
            r.get("telefone_conversa", ""),
            r.get("telefone_cadastro", ""),
            r["opened_at_str"],
            r["atendente_nome"],
            r["departamento_nome"],
            ", ".join(r["categorias"]),
            r["protocol"],
            r["status_label"],
            _formula_whatsapp(total + 1),  # +1: a linha 1 é o cabeçalho
        ])

    _aba_configuracao(
        wb,
        exportado_em=exportado_em,
        recorte=recorte,
        total=total,
        com_numero=com_numero,
        mensagem=mensagem,
    )
    # A aba de dados fica ativa ao abrir: a configuração é ajuste, não destino.
    wb.active = wb["Atendimentos"]
    return wb
