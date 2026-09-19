"""Spike R2 — a geometria do cabo existe na API do IXC? (evidência reproduzível)

O plano afirmava que `df_coordenada` não existia, sem nunca ter testado. Este
script é o teste: ele bate na API de produção e imprime a resposta crua, os
volumes e a distância entre CTO e vértice de cabo. Resultado de 2026-09-19 em
`docs/massivas-rota-e-cabos-plano.md` (R2).

Como rodar (a partir da sua máquina, contra o pod de produção):

    scp docs/spikes/r2_geometria_cabo_spike.py paulo@<host>:/tmp/spike.py
    ssh paulo@<host> "POD=\\$(kubectl get pod -n dashboard-velus -l component=web \\
        -o jsonpath='{.items[0].metadata.name}'); \\
        kubectl cp /tmp/spike.py dashboard-velus/\\$POD:/tmp/spike.py && \\
        kubectl exec -n dashboard-velus \\$POD -- \\
        python manage.py shell -v0 -c \\"exec(open('/tmp/spike.py').read())\\""

É só leitura: nenhuma chamada aqui escreve no IXC nem no nosso banco.
"""

import json
from apps.shared.context import set_current_organization
from apps.tenancy.models import Organization, OrganizationDataSource
set_current_organization(Organization.objects.first())
org = Organization.objects.first()

ds = OrganizationDataSource.objects.filter(
    organization=org, source_type="IXC", capability="NETWORK_ELEMENTS", is_active=True
).first() or OrganizationDataSource.objects.filter(
    organization=org, source_type="IXC", is_active=True
).first()
creds = ds.get_credentials()
print("datasource:", ds.capability, "base:", creds.get("base_url"))

from apps.integrations.ixc.client import IxcHttpClient

def amostra(resource, body_filter=None, n=3):
    print(f"\n===== {resource} filtro={body_filter} =====")
    try:
        with IxcHttpClient(
            base_url=creds["base_url"], user_id=creds["user_id"], api_token=creds["api_token"]
        ) as c:
            rows = []
            for i, r in enumerate(c.paginate_ixc(resource, body_filter=body_filter, page_size=50)):
                rows.append(r)
                if i + 1 >= n:
                    break
        if not rows:
            print("  (vazio)")
            return []
        print("  campos:", sorted(rows[0].keys()))
        for r in rows:
            print("  ", json.dumps(r, ensure_ascii=False)[:400])
        return rows
    except Exception as e:
        print("  ERRO:", type(e).__name__, str(e)[:300])
        return []

# 1. A tabela de coordenadas: existe endpoint? o que devolve?
amostra("df_elemento_coordenada", {"qtype": "df_elemento_coordenada.id", "query": "0", "oper": ">"})
amostra("df_elemento_coordenada")

# 2. Um cabo: quais campos tem?
cabos = amostra("df_elemento", {"qtype": "df_elemento.tipo", "query": "CB", "oper": "="}, n=2)

# 3. Coordenadas DAQUELE cabo
if cabos:
    cid = cabos[0].get("id")
    for campo in ("df_elemento_coordenada.id_elemento", "df_elemento_coordenada.id_df_elemento"):
        amostra("df_elemento_coordenada", {"qtype": campo, "query": str(cid), "oper": "="}, n=5)

# 4. Candidatos de tabela de coordenada pura
for recurso in ("df_coordenada", "df_elemento_coordenadas", "df_ponto", "df_elemento_ponto"):
    amostra(recurso, n=1)

# --- parte 2: cobertura ---
import json
from collections import defaultdict
from apps.shared.context import set_current_organization
from apps.tenancy.models import Organization, OrganizationDataSource
set_current_organization(Organization.objects.first())
org = Organization.objects.first()
ds = OrganizationDataSource.objects.filter(organization=org, source_type="IXC", is_active=True).first()
creds = ds.get_credentials()
from apps.integrations.ixc.client import IxcHttpClient
import json as _json

def total_de(resource, body_filter=None):
    """Lê só o campo `total` da primeira página."""
    with IxcHttpClient(base_url=creds["base_url"], user_id=creds["user_id"], api_token=creds["api_token"]) as c:
        body = dict(body_filter or {})
        body["page"] = "1"; body["rp"] = "1"
        payload = c._fetch_page(resource, body)
        return payload.get("total")

def todos(resource, body_filter=None, limite=100000):
    out = []
    with IxcHttpClient(base_url=creds["base_url"], user_id=creds["user_id"], api_token=creds["api_token"]) as c:
        for r in c.paginate_ixc(resource, body_filter=body_filter, page_size=1000):
            out.append(r)
            if len(out) >= limite:
                break
    return out

print("total df_coordenada:", total_de("df_coordenada"))
print("total df_elemento_coordenada:", total_de("df_elemento_coordenada"))
print("total df_elemento (todos):", total_de("df_elemento"))
for tipo in ("CB", "CA", "CS", "PT", "AR"):
    print(f"  df_elemento tipo={tipo}:", total_de("df_elemento", {"qtype": "df_elemento.tipo", "query": tipo, "oper": "="}))

vinculos = todos("df_elemento_coordenada")
print("vinculos baixados:", len(vinculos))
por_elemento = defaultdict(list)
for v in vinculos:
    por_elemento[v["id_elemento"]].append((int(v.get("sequencia") or 0), v["id_coordenada"]))

cabos = todos("df_elemento", {"qtype": "df_elemento.tipo", "query": "CB", "oper": "="})
print("cabos:", len(cabos))
com_geo = [c for c in cabos if len(por_elemento.get(c["id"], [])) >= 2]
print("cabos com >=2 vertices:", len(com_geo))
vertices = [len(por_elemento.get(c["id"], [])) for c in cabos]
vertices.sort()
if vertices:
    print("vertices por cabo: min", vertices[0], "mediana", vertices[len(vertices)//2], "max", vertices[-1])
    print("cabos sem nenhum vertice:", sum(1 for v in vertices if v == 0))

coords = {c["id"]: (float(c["latitude"]), float(c["longitude"])) for c in todos("df_coordenada")}
print("coordenadas baixadas:", len(coords))

# Exemplo de traçado real
if com_geo:
    exemplo = com_geo[0]
    pts = [coords.get(cid) for _, cid in sorted(por_elemento[exemplo["id"]])]
    print("\nexemplo de cabo:", exemplo["descricao"], "projeto", exemplo["id_projeto"])
    print("  pontos:", pts[:8], "..." if len(pts) > 8 else "")

# As caixas do InMap (CA/CS) têm coordenada? Dá pra casar com a CTO do rad_caixa_ftth?
caixas = todos("df_elemento", {"qtype": "df_elemento.tipo", "query": "CA", "oper": "="})
print("\ncaixas de emenda (CA):", len(caixas), "com coordenada:",
      sum(1 for c in caixas if por_elemento.get(c["id"])))
if caixas:
    print("  exemplo:", json.dumps(caixas[0], ensure_ascii=False)[:200])
    cid = por_elemento.get(caixas[0]["id"])
    if cid:
        print("  ponto:", coords.get(sorted(cid)[0][1]))

# --- parte 3: vínculo cabo↔CTO por geometria ---
from collections import defaultdict
from apps.shared.context import set_current_organization
from apps.tenancy.models import Organization, OrganizationDataSource
set_current_organization(Organization.objects.first())
org = Organization.objects.first()
ds = OrganizationDataSource.objects.filter(organization=org, source_type="IXC", is_active=True).first()
creds = ds.get_credentials()
from apps.integrations.ixc.client import IxcHttpClient
from apps.network.domain.outage import haversine_meters
from apps.network.infrastructure.models import NetworkElement

def todos(resource, body_filter=None):
    out = []
    with IxcHttpClient(base_url=creds["base_url"], user_id=creds["user_id"], api_token=creds["api_token"]) as c:
        for r in c.paginate_ixc(resource, body_filter=body_filter, page_size=1000):
            out.append(r)
    return out

coords = {c["id"]: (float(c["latitude"]), float(c["longitude"])) for c in todos("df_coordenada")}
vinc = todos("df_elemento_coordenada")
cabos = {c["id"]: c for c in todos("df_elemento", {"qtype": "df_elemento.tipo", "query": "CB", "oper": "="})}

por_elemento = defaultdict(list)
for v in vinc:
    por_elemento[v["id_elemento"]].append((int(v.get("sequencia") or 0), v["id_coordenada"]))

# Todos os vértices de cabo, com o cabo de origem
vertices = []
for cabo_id in cabos:
    for _, cid in sorted(por_elemento.get(cabo_id, [])):
        p = coords.get(cid)
        if p:
            vertices.append((p, cabo_id))
print("vertices de cabo:", len(vertices))

ctos = list(
    NetworkElement.objects.filter(
        organization=org, kind=NetworkElement.Kind.CTO,
        latitude__isnull=False, longitude__isnull=False,
    ).values_list("external_id", "name", "latitude", "longitude")
)
print("CTOs com coordenada:", len(ctos))

import bisect
# Busca grosseira por caixa de grade pra não fazer 1445 x 12000 haversines completos
grade = defaultdict(list)
PASSO = 0.005  # ~500 m
for (lat, lon), cabo_id in vertices:
    grade[(int(lat / PASSO), int(lon / PASSO))].append(((lat, lon), cabo_id))

distancias = []
sem_vizinho = 0
exemplos = []
for ext, nome, lat, lon in ctos:
    lat, lon = float(lat), float(lon)
    cand = []
    gi, gj = int(lat / PASSO), int(lon / PASSO)
    for di in (-1, 0, 1):
        for dj in (-1, 0, 1):
            cand.extend(grade.get((gi + di, gj + dj), []))
    if not cand:
        sem_vizinho += 1
        continue
    melhor = min(cand, key=lambda v: haversine_meters((lat, lon), v[0]))
    d = haversine_meters((lat, lon), melhor[0])
    distancias.append(d)
    if len(exemplos) < 5:
        exemplos.append((nome or ext, round(d, 1), cabos[melhor[1]]["descricao"]))

distancias.sort()
def pct(p):
    return round(distancias[int(len(distancias) * p)], 1) if distancias else None
print("CTOs sem vértice de cabo por perto (>1km):", sem_vizinho)
print("distância CTO → vértice de cabo mais próximo:")
print("  p10", pct(0.10), "mediana", pct(0.50), "p90", pct(0.90), "max", round(distancias[-1],1) if distancias else None)
print("  dentro de 10 m:", sum(1 for d in distancias if d <= 10), "de", len(distancias))
print("  dentro de 30 m:", sum(1 for d in distancias if d <= 30))
print("exemplos:")
for e in exemplos:
    print("  ", e)
