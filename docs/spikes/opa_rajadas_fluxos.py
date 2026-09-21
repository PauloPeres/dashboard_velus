"""Onde os fluxos do Opa mandam duas ou mais mensagens seguidas.

Lê a estrutura dos 33 fluxos (endpoint interno `fluxos-comunicacao/card`) e
percorre o grafo de cada um. Uma RAJADA é uma sequência de nós que enviam
mensagem sem nada no meio que espere o cliente responder — é exatamente o que dá
para juntar num texto só.

A travessia atravessa `eflow` (um fluxo chamando outro), porque é lá que está o
caso difícil: ninguém vê a emenda olhando um fluxo de cada vez.
"""
import json
from collections import defaultdict

FLUXOS = json.load(open("fluxos.json"))["rows"]
por_id = {f["_id"]: f for f in FLUXOS}
por_nome = {f["nome"]: f for f in FLUXOS}


def indexa(est):
    """`next` aponta para a POSIÇÃO do nó no array — descoberto lendo o JSON:
    em "00 - Verifica se é cliente", `init.next = 1` é o nó da posição 1, e o
    `cond.next1 = 2` é o `msg` da posição 2, que não tem `indice` nenhum.

    `indice`/`cardIndex` existem em parte dos nós e às vezes discordam da
    posição; por isso a posição tem prioridade.
    """
    idx = {}
    for n in est:
        for chave in ("indice", "cardIndex"):
            v = n.get(chave)
            if v not in (None, ""):
                idx.setdefault(str(v), n)
    for i, n in enumerate(est):
        idx[str(i)] = n
    return idx


def textos_do_no(n):
    """O que este nó ENVIA, em texto legível. Vazio = não envia nada."""
    t = n.get("type")
    if t == "msg":
        return [m.get("value", "") for m in (n.get("mensagens") or []) if m.get("value")]
    if t == "perg":
        return [n.get("pergunta", "")]
    if t == "opt_in_opt_out":
        return [n.get("mensagemOptIn", "")]
    if t == "pesquisa-satisfacao":
        return [n.get("pergunta", "")]
    return []


# Nós que ESPERAM o cliente: fecham a rajada.
ESPERA = {"perg", "opt_in_opt_out", "pesquisa-satisfacao"}


def caminhar(fluxo, no, idx, rajada, vistos, saida, profundidade=0):
    if profundidade > 60:
        return
    while no is not None:
        chave = (fluxo["_id"], id(no))
        if chave in vistos:
            break
        vistos = vistos | {chave}
        tipo = no.get("type")

        if tipo == "eflow":
            # Fluxo chamando fluxo: a rajada NÃO termina aqui — o cliente não
            # é perguntado nada no meio, ele só recebe mais mensagens.
            destino = por_id.get(no.get("eflow_id"))
            if destino is None:
                break
            est2 = json.loads(destino["estrutura"])
            idx2 = indexa(est2)
            inicio = next((x for x in est2 if x.get("type") == "init"), None)
            prox = idx2.get(str(inicio.get("next"))) if inicio else None
            caminhar(destino, prox, idx2, rajada, vistos, saida, profundidade + 1)
            return

        envios = [t for t in textos_do_no(no) if t.strip()]
        for t in envios:
            rajada.append((fluxo["nome"], tipo, t))

        if tipo in ESPERA or not no.get("next"):
            break
        no = idx.get(str(no.get("next")))

    if len(rajada) >= 2:
        saida.append(list(rajada))


def ramifica(fluxo, est, idx):
    """Cada caminho do grafo vira uma travessia — inclusive os dois lados do cond."""
    # Começa de TODOS os nós, não só do init: seguir apenas o caminho principal
    # esconderia as rajadas que só acontecem num ramo de condição ou numa opção
    # de menu — e são elas que o cliente vive na prática.
    saida = []
    for n in est:
        caminhar(fluxo, n, idx, [], frozenset(), saida)
    return saida


def resumo(t, n=70):
    return " ".join(str(t).split())[:n]


todas = []
for f in FLUXOS:
    est = json.loads(f["estrutura"])
    idx = indexa(est)
    for r in ramifica(f, est, idx):
        todas.append(r)

# Deduplica rajadas iguais (mesmo par de textos por caminhos diferentes).
unicas = {}
for r in todas:
    chave = tuple(resumo(t, 120) for _, _, t in r)
    if chave not in unicas or len(r) > len(unicas[chave]):
        unicas[chave] = r

pares_entre_fluxos = [r for r in unicas.values() if len({f for f, _, _ in r}) > 1]
print(f"RAJADAS ENCONTRADAS {len(unicas)} | DELAS ENTRE FLUXOS DIFERENTES {len(pares_entre_fluxos)}")

print("\n" + "=" * 78)
print("RAJADAS DENTRO DO MESMO FLUXO")
print("=" * 78)
for r in sorted(unicas.values(), key=lambda x: -len(x)):
    if len({f for f, _, _ in r}) > 1:
        continue
    print(f"\n[{r[0][0]}] {len(r)} mensagens seguidas:")
    for i, (_, tipo, t) in enumerate(r, 1):
        print(f"   {i}. ({tipo}) {resumo(t)}")

print("\n" + "=" * 78)
print("RAJADAS QUE ATRAVESSAM FLUXO (um fluxo chama outro)")
print("=" * 78)
for r in sorted(pares_entre_fluxos, key=lambda x: -len(x)):
    print(f"\n{len(r)} mensagens seguidas, atravessando "
          f"{' -> '.join(dict.fromkeys(f for f, _, _ in r))}:")
    for i, (fl, tipo, t) in enumerate(r, 1):
        print(f"   {i}. [{fl}] ({tipo}) {resumo(t)}")

print("\n" + "=" * 78)
print("OUTRAS RAJADAS QUE O GRAFO NÃO MOSTRA COMO SEQUÊNCIA DE NÓS")
print("=" * 78)

print("\n-- UM NÓ QUE JÁ MANDA VÁRIAS MENSAGENS (o disparo é do próprio nó) --")
for f in FLUXOS:
    for n in json.loads(f["estrutura"]):
        if n.get("type") == "msg":
            vals = [m.get("value") for m in (n.get("mensagens") or []) if m.get("value")]
            if len(vals) > 1:
                print(f"\n[{f['nome']}] {len(vals)} mensagens num nó só:")
                for i, v in enumerate(vals, 1):
                    print(f"   {i}. {resumo(v)}")

print("\n-- ERRO DE OPÇÃO: manda o aviso E repete a pergunta (2 por engano do cliente) --")
for f in FLUXOS:
    for n in json.loads(f["estrutura"]):
        if n.get("type") == "perg" and n.get("msg_erro"):
            erros = n["msg_erro"] if isinstance(n["msg_erro"], list) else [n["msg_erro"]]
            print(f"\n[{f['nome']}]")
            print(f"   1. (erro) {resumo(erros[0])}")
            print(f"   2. (repete) {resumo(n.get('pergunta'))}")

print("\n-- LIMITE DE ERRO: manda a mensagem E pula para outro fluxo, que fala de novo --")
for f in FLUXOS:
    for n in json.loads(f["estrutura"]):
        msg = n.get("msg_qtd_erro_excedida")
        destino = por_id.get(n.get("executarFlowLimiteErros"))
        if msg and destino:
            print(f"\n[{f['nome']}] ({n.get('type')}) -> [{destino['nome']}]")
            print(f"   1. {resumo(msg)}")
            prim = next(
                (t for x in json.loads(destino["estrutura"]) for t in textos_do_no(x) if t.strip()),
                "",
            )
            if prim:
                print(f"   2. {resumo(prim)}")
