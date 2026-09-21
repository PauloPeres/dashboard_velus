"""Link de campo — a massiva no mapa, aberta por quem não tem login.

O técnico na rua precisa ver **por onde o cabo corre** e **onde estão as
caixas**, não só um ponto no Google Maps. Mas ele não tem conta no dashboard, e
criar uma para cada pessoa que entra e sai seria trocar um problema por outro.

A saída, decidida com o operador em 21/09/2026: **link assinado com validade de
24 horas**, escopado a uma massiva só.

O que isso significa, dito por inteiro, porque é uma porta aberta na frente do
sistema:

- **quem tem o link entra**, sem senha. É a mesma natureza de um convite de
  reunião ou de um link de rastreio de encomenda;
- por isso ele **expira em 24 h** e vale para **uma massiva**. Encaminhado no
  grupo do WhatsApp, o dano é limitado ao que aquele evento mostra, e some
  sozinho no dia seguinte;
- a assinatura usa a `SECRET_KEY` do Django, então **um link forjado não abre**:
  trocar o id da massiva na URL invalida a assinatura.

O que a tela de campo mostra é deliberadamente menos que a aba: mapa, caixas,
cabo e contagem. **Sem nome, documento ou telefone de cliente** — o técnico
precisa saber onde cavar, não quem mora ali.
"""

from __future__ import annotations

from typing import Any

from django.core import signing

# Validade do link. Vinte e quatro horas cobre o turno e a virada de plantão —
# e garante que um link esquecido num grupo não sirva na semana que vem.
VALIDADE_SEGUNDOS = 24 * 60 * 60

# Sal próprio: um token de campo não pode ser reaproveitado por nenhum outro
# mecanismo assinado do sistema, nem o contrário.
_SALT = "massivas.campo.v1"


def assinar(outage_id: int, organization_id: int) -> str:
    """Token que abre **uma** massiva, de **uma** organização.

    A organização entra na assinatura, e não só o id do evento: sem ela, um id
    válido noutra base abriria a massiva errada no dia em que o sistema tiver
    mais de um cliente.
    """
    return signing.dumps(
        {"o": int(outage_id), "org": int(organization_id)}, salt=_SALT
    )


def ler(token: str) -> dict[str, Any] | None:
    """Devolve `{"o": id, "org": id}` ou `None` — expirado, forjado ou torto.

    Um único `None` para todos os casos de propósito: quem recebe a resposta na
    tela não precisa saber se o link expirou ou foi adulterado, e distinguir os
    dois ajudaria só quem está tentando forjar.
    """
    try:
        dados = signing.loads(token, salt=_SALT, max_age=VALIDADE_SEGUNDOS)
    except signing.BadSignature:
        return None
    if not isinstance(dados, dict) or "o" not in dados or "org" not in dados:
        return None
    return dados
