"""Preenche canal/origem dos atendimentos ja sincronizados, sem tocar a rede.

Os tres campos ja estavam em `raw_extras` (`canal_id` e `origem`) desde sempre —
o sync guardava o cru e ninguem lia. Promove-los a coluna e trabalho de banco,
nao de API: nao ha uma unica chamada ao Opa aqui, e por isso vale uma data
migration em vez de um comando que alguem precisaria lembrar de rodar.

Migration de dados sem `reverse`: o inverso e zerar colunas novas, o que a
propria remocao delas ja faria.
"""

from __future__ import annotations

from django.db import migrations

# Lotes pra nao carregar a tabela inteira na memoria nem fazer um UPDATE gigante
# (a base de producao tem ~34 mil atendimentos no momento desta migration).
_CHUNK = 2000


def _extrair(raw_extras: dict | None) -> tuple[str, str, str]:
    extras = raw_extras or {}
    canal = extras.get("canal_id") or ""
    origem = extras.get("origem")
    tipo = ref = ""
    if isinstance(origem, dict):
        tipo = origem.get("tipo") or ""
        dados = origem.get("dados")
        if isinstance(dados, dict):
            ref = dados.get("id") or ""
    return str(canal), str(tipo), str(ref)


def preencher(apps, schema_editor) -> None:
    Atendimento = apps.get_model("atendimento", "Atendimento")

    pendentes = []
    queryset = Atendimento.objects.exclude(raw_extras={}).only("id", "raw_extras")
    for atendimento in queryset.iterator(chunk_size=_CHUNK):
        canal, tipo, ref = _extrair(atendimento.raw_extras)
        if not (canal or tipo or ref):
            continue
        atendimento.canal_external_id = canal[:128]
        atendimento.origem_tipo = tipo[:64]
        atendimento.origem_ref = ref[:128]
        pendentes.append(atendimento)
        if len(pendentes) >= _CHUNK:
            Atendimento.objects.bulk_update(
                pendentes, ["canal_external_id", "origem_tipo", "origem_ref"]
            )
            pendentes = []

    if pendentes:
        Atendimento.objects.bulk_update(
            pendentes, ["canal_external_id", "origem_tipo", "origem_ref"]
        )


def preencher_mensagens(apps, schema_editor) -> None:
    """Mesma promocao pras mensagens que o drill-down ja tinha trazido.

    Sao poucas (as de 1 ano de cliques), mas ficariam com canal vazio ate o
    backfill de volume passar por cima delas — e as que estao fora da janela do
    backfill nunca seriam corrigidas.
    """
    Mensagem = apps.get_model("atendimento", "Mensagem")

    pendentes = []
    queryset = Mensagem.objects.exclude(raw_extras={}).only("id", "raw_extras")
    for mensagem in queryset.iterator(chunk_size=_CHUNK):
        extras = mensagem.raw_extras or {}
        canal = str(extras.get("canalComunicacao") or "")
        fora = extras.get("envioForaJanela24h")
        if not canal and fora is None:
            continue
        mensagem.canal_external_id = canal[:128]
        mensagem.fora_janela_24h = fora if isinstance(fora, bool) else None
        pendentes.append(mensagem)
        if len(pendentes) >= _CHUNK:
            Mensagem.objects.bulk_update(
                pendentes, ["canal_external_id", "fora_janela_24h"]
            )
            pendentes = []

    if pendentes:
        Mensagem.objects.bulk_update(pendentes, ["canal_external_id", "fora_janela_24h"])


class Migration(migrations.Migration):

    dependencies = [
        ("atendimento", "0005_canalcomunicacao_atendimento_canal_external_id_and_more"),
    ]

    operations = [
        migrations.RunPython(preencher, migrations.RunPython.noop),
        migrations.RunPython(preencher_mensagens, migrations.RunPython.noop),
    ]
