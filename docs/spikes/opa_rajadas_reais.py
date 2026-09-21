"""Rajadas de verdade: mensagens nossas, uma seguida da outra, na mesma conversa.

O grafo dos fluxos diz onde a rajada PODE acontecer; isto diz quantas vezes ela
ACONTECE, e quanto daria para economizar juntando cada rajada numa mensagem só.

Uma rajada é uma sequência de mensagens enviadas por nós, na mesma conversa, sem
nenhuma mensagem do cliente no meio. O tempo não entra na definição: duas
mensagens nossas separadas por dez minutos e nenhum recebimento continuam sendo
duas notificações seguidas no celular de quem recebe.
"""
import json, re, unicodedata
from collections import Counter, defaultdict

import django  # noqa
from apps.integrations.opa.atendimento import OpaAtendimentoSource
from apps.integrations.shared.enums import Capability, SourceType
from apps.shared.context import set_current_organization
from apps.shared.decorators import allow_cross_tenant
from apps.tenancy.models import Organization, OrganizationDataSource

PAGINAS = 20
PAGE = 1000
TARIFA = 0.035


def norm(t):
    t = unicodedata.normalize("NFKC", str(t or "")).strip().lower()
    t = re.sub(r"<br\s*/?>", " ", t)
    return re.sub(r"\s+", " ", t)


def rotulo(t, n=55):
    return norm(t)[:n] or "(sem texto)"


org = Organization.objects.get(slug="velus")
set_current_organization(org)
ds = allow_cross_tenant(reason="analise de rajadas")(
    lambda: OrganizationDataSource.objects.filter(
        organization=org,
        source_type=SourceType.OPA.value,
        capability=Capability.ATENDIMENTO.value,
        is_active=True,
    ).first()
)()
creds = ds.get_credentials()
src = OpaAtendimentoSource(base_url=creds["base_url"], token=creds["token"])

conversas = defaultdict(list)
dias = set()
with src._client_factory() as client:
    lo, hi = 0, 4_000_000
    while lo < hi:
        mid = (lo + hi) // 2
        r = client.get("atendimento/mensagem", json={"filter": {}, "options": {"limit": 1, "skip": mid}})
        if (r.get("data") if isinstance(r, dict) else None):
            lo = mid + 1
        else:
            hi = mid
    skip = max(0, lo - PAGINAS * PAGE)
    print("TOTAL NA COLECAO", lo)
    for _ in range(PAGINAS):
        r = client.get("atendimento/mensagem", json={"filter": {}, "options": {"limit": PAGE, "skip": skip}})
        itens = r.get("data") if isinstance(r, dict) else None
        if not itens:
            break
        for raw in itens:
            if raw.get("data"):
                dias.add(raw["data"][:10])
            conversas[raw.get("id_rota") or "?"].append(raw)
        skip += len(itens)

print(f"CONVERSAS {len(conversas)} | DIAS {min(dias)} a {max(dias)} ({len(dias)})")

tamanhos = Counter()
pares = Counter()
trincas = Counter()
enviadas = 0
excesso = 0
conversas_com_rajada = 0
# Uma conversa da amostra tinha uma rajada de 598 mensagens — é disparo em
# massa, não atendimento, e deixá-la dentro faria a média mentir. Fica contada à
# parte, em vez de sumir.
gigantes = {r: m for r, m in conversas.items() if len(m) > 100}
if gigantes:
    print(f"\nCONVERSAS GIGANTES (>100 msgs, fora da conta): {len(gigantes)} "
          f"com {sum(len(m) for m in gigantes.values())} mensagens")
conversas = {r: m for r, m in conversas.items() if len(m) <= 100}

for rota, msgs in conversas.items():
    msgs.sort(key=lambda m: (m.get("data") or ""))
    corrida = []
    teve = False
    for m in msgs:
        if m.get("tipoDestinatario") == "clientes_users":
            enviadas += 1
            corrida.append(m)
            continue
        if len(corrida) >= 2:
            teve = True
            tamanhos[len(corrida)] += 1
            excesso += len(corrida) - 1
            chaves = [rotulo(x.get("mensagem")) for x in corrida]
            for i in range(len(chaves) - 1):
                pares[(chaves[i], chaves[i + 1])] += 1
            for i in range(len(chaves) - 2):
                trincas[tuple(chaves[i:i + 3])] += 1
        corrida = []
    if len(corrida) >= 2:
        teve = True
        tamanhos[len(corrida)] += 1
        excesso += len(corrida) - 1
        chaves = [rotulo(x.get("mensagem")) for x in corrida]
        for i in range(len(chaves) - 1):
            pares[(chaves[i], chaves[i + 1])] += 1
        for i in range(len(chaves) - 2):
            trincas[tuple(chaves[i:i + 3])] += 1
    conversas_com_rajada += 1 if teve else 0

total_rajadas = sum(tamanhos.values())
print(f"\nENVIADAS {enviadas} | RAJADAS {total_rajadas} | "
      f"CONVERSAS COM RAJADA {conversas_com_rajada} de {len(conversas)}")
print(f"MENSAGENS EM EXCESSO (se cada rajada virasse 1) {excesso} "
      f"= {excesso*100//max(enviadas,1)}% do que enviamos")
dias_n = max(len(dias), 1)
print(f"POR DIA: {excesso/dias_n:.0f} mensagens · R$ {excesso/dias_n*30*TARIFA:.2f}/mês")
print("\nTAMANHO DAS RAJADAS:")
for k in sorted(tamanhos):
    print(f"  {k} mensagens seguidas: {tamanhos[k]}")

print("\nPARES MAIS FREQUENTES (A logo depois B, sem o cliente falar no meio):")
for (a, b), c in pares.most_common(25):
    print(f"\n{c:>5}x")
    print(f"      1. {a}")
    print(f"      2. {b}")

print("\nTRINCAS MAIS FREQUENTES:")
for t, c in trincas.most_common(10):
    print(f"\n{c:>5}x")
    for i, x in enumerate(t, 1):
        print(f"      {i}. {x}")
