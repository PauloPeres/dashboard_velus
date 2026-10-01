# Backtest da massiva 359 — onde o sistema mandou o técnico, e onde era

**30/09/2026.** Massiva 359: OLT 1, 490 clientes, 42 PONs. O rompimento foi num
cabo **FIBRA AS80 72FO 5** (geometria 1469), no ponto `-23.496056, -47.510700`,
informado pelo Paulo durante o evento.

O cabo **estava** no cadastro, a 13 m do ponto. Mesmo assim, a tela da massiva:

- pôs o "comece por aqui" no **POP Velus Sorocaba, a 1.155 m** do rompimento,
  sem trecho nem X;
- listou 6 cabos candidatos por proximidade, **nenhum deles o 72FO**;
- desenhou o 72FO só como linha cinza de 1 px, no meio de 250 cabos de contexto.

## Método

Retrato da massiva tirado de produção (quedas, horários, PON, OLT, estado das
conexões por CTO, planta completa) e rodado fora dela, com o **mesmo código de
domínio** (`build_graph`, `distances_from`, `calcular`). Cada algoritmo foi
medido pela distância entre o que ele aponta e o ponto real. Para trechos, a
medida é do ponto até o pedaço de cabo apontado.

## Resultado

| Algoritmo | Distância do ponto real |
|---|---|
| Produção (ponto comum a 100% das caixas) | 1.155 m — aponta o POP, sem trecho |
| Centróide das caixas fora | 928 m |
| 1ª caixa fora saindo do POP | 1.186 m |
| Trecho mais fundo que cobre ≥ 90% das caixas fora | 165 m (80% → 240 m; 95% → 841 m: instável) |
| **Verossimilhança por trecho** | **13 m** — 72FO 5, CEA → A16-SP01 (635 m), 1º do ranking |

A verossimilhança foi testada contando de cinco formas (por caixa, por cliente,
por PON, F1 e F2). Todas deram o mesmo trecho em 1º lugar. Dentro dele, o
rompimento fica a 136 m pelo cabo a partir da A16-SP01.

### Estresse (para não ser sorte de um caso)

| Cenário | Produção | Cobertura ≥ 90% | Verossimilhança |
|---|---|---|---|
| Só 5 das 220 caixas fora vistas | 0% | 5% | 97% |
| Só 20 vistas | 0% | 13% | 100% |
| +60 caixas aleatórias caídas por outro motivo | 0% | 0% | 100% |
| 40 vistas + 20 de ruído | 0% | 0% | 100% |
| Pior caso: metade das caixas fora lidas como "no ar" | — | — | 92% (quando erra, cai no trecho vizinho, a 109 m) |

"Acerto" = o trecho escolhido passa a até 50 m do ponto real.

## Por que a produção errou: dois bugs

1. **Exigir 100% das caixas.** 13 das 220 caixas fora não pendiam do 72FO.
   Quase todas eram caixas mistas, com clientes de várias PONs e **um** cliente
   caído (a A05-SP05 tem clientes em 12 PONs). Uma só delas bastava para jogar
   o ponto comum até o POP.
2. **Caixa vazia contava como "no ar".** Abaixo do trecho real havia 114 caixas
   "no ar", e 109 delas não tinham nenhum cliente ativo. Com isso, a regra "todos
   abaixo caíram" nunca se confirmava.

## Achado: a massiva é binária por PON

42 PONs da OLT 1 caíram de 80% a 100%, e todas as outras ficaram em 0%.
Nenhuma ficou no meio (só a PON 160, com 2 de 3, e a 223, com 2 de 21, de
ruído). É a assinatura física de um cabo que leva várias PONs rompido antes dos
splitters: a PON é uma fibra, e fibra cortada derruba a PON inteira.

## O que mudou no sistema

- **Rota do técnico por verossimilhança** (`network/domain/repair_route.py`,
  regra `verossimilhanca-v1`). Cada elemento da planta é uma hipótese ("o
  rompimento é logo acima daqui"), e a nota compara as caixas fora abaixo dele
  com as caixas **com cliente online** abaixo dele. Não há parâmetro para
  ajustar. A tela mostra as 3 melhores hipóteses, uma por cabo, com as contas.
  O "comece por aqui" e o X só são afirmados quando a 1ª explica ≥ 80% das
  caixas fora com ≥ 80% de pureza.
- **"No ar" = caixa com cliente ONLINE agora**, sem contar os clientes da
  própria massiva (`ctos_com_cliente_no_ar`).
- **O cabo da hipótese entra no desenho** como candidato, grosso. O trecho vai
  sobre o cabo, e o X fica no meio do trecho, medido pelo comprimento.
- **Padrão por PON** (`network/domain/pon_pattern.py`). A tela e a mensagem
  dizem "Corte de tronco: N PONs inteiras fora", "OLT inteira fora" ou "PON
  inteira fora (PON X)". Quando a forma não bate com nenhum desses, fica calado.
- **Onde rompeu.** O detalhe da massiva (e a fila de causa) aceita o link do
  Google Maps ou "lat, long". Registrado o ponto, o sistema mede o erro de cada
  resposta (trecho da 1ª hipótese, "comece por aqui", X, cabo candidato) e a
  posição do trecho certo no ranking, e guarda a evidência que o algoritmo viu.
- **Backtest contínuo.** `python manage.py aferir_rompimentos --org velus`
  refaz a aferição de todas as massivas com rompimento registrado, usando a
  evidência guardada. É para rodar antes de subir uma mudança de regra.
- **Cabos candidatos 100× mais rápido.** Na 359, `compute_cabos_candidatos`
  levava 11 s por chamada, e a lista de massivas abertas chama essa função a
  cada auto-refresh. Com o filtro por caixa envolvente, passou a 97 ms, com
  resultado idêntico (291 candidatos, conferido em produção).

Medido na 359 com o código novo, em produção e só lendo:

- a 1ª hipótese fica a 13 m do rompimento (acertou), e o trecho certo é o 1º do
  ranking;
- o "comece por aqui" fica na A16-SP01, a 109 m do rompimento;
- o X fica a 143 m do rompimento;
- o padrão por PON dá "Corte de tronco: 43 PONs inteiras fora".

## Limites

- **É uma massiva só.** O estresse ajuda, mas só o campo "onde rompeu",
  preenchido em mais massivas, diz se a regra generaliza. Os limiares de 80%
  são ponto de partida.
- **O trecho tem 635 m**, e dentro dele não há dado para afinar mais.
- **A árvore sai do caminho mais curto no desenho**, não da fibra: o caminho do
  POP até a CEA passa por cabos 06FO/12FO. Aqui não atrapalhou.

## Próximos passos

- Guardar a **distância da ONU** (`ont_distance_m`). O painel da OLT já a
  entrega, mas o sistema a descarta hoje. Com ela, o X vira uma metragem
  medida ao longo do cabo.
- Sondar se o InMap expõe as **fusões** (qual fibra segue para qual splitter).
  Com elas, a árvore vira a planta de verdade.
- Calibrar os limiares com as massivas que tiverem o rompimento registrado.

## Teste cego na planta inteira (01/10/2026)

O Paulo pediu uma revisão: o algoritmo não podia estar "forçando" o ponto da
359. Três verificações.

**1. Nada da 359 no código que calcula.** O ponto, o cabo 1469 e as caixas
A16-SP01/CEA aparecem só em comentário de histórico, em teste e neste
documento. O único lugar em que a coordenada aparecia literalmente era o texto
de exemplo do campo "Onde rompeu", e ela foi trocada por uma neutra.

**2. Rompimentos sorteados.** São 300 por cenário, num trecho sorteado da
planta inteira (o trecho tem de ter entre 3 e 300 caixas com cliente abaixo).
O "ponto real" também é sorteado, ao longo do trecho. O algoritmo recebe só a
planta, quem caiu e quem está online; o ponto sorteado serve apenas para medir
o erro depois. Há ruído realista: corte parcial (só parte das caixas abaixo
cai) e caixas que caem por outro motivo em qualquer lugar da planta.

O primeiro resultado mostrou que, em 31% dos cortes limpos, o rompimento estava
num pedaço *de baixo* de uma sequência sem nenhuma caixa com cliente no meio
(junção, emenda, caixa vazia). Ali nenhum dado separa um pedaço do outro, e a
regra apontava só o primeiro. Em 100% desses casos o rompimento estava dentro
da sequência. Desde então o trecho vai da última caixa boa até a primeira caixa
caída (`_estender_pela_cadeia`), e o resultado foi este:

| Cenário | Acerta o trecho (≤50 m) | Trecho a percorrer (mediana) | Regra antiga (prefixo 100%) |
|---|---|---|---|
| Corte limpo | 100% | 276 m | 15% |
| Parcial (60–100% das caixas abaixo caem) | 96% | 268 m | 8% |
| Parcial + até 5 caixas caídas por outro motivo | 89% | 225 m | 0,3% |
| Duro: 40–100% + até 15 de ruído | 80% | 212 m | 0% |

**3. Massivas reais.** Rodado só lendo, nas 25 massivas mais recentes com 8 ou
mais clientes, o algoritmo dá respostas diferentes para cada evento, com
cabos, caixas, distâncias e confiança diferentes. Uma ressalva: ali "online" é
quem está online hoje, não no dia do evento.

### Os limiares de "afirmar o X"

`EXPLICA_MINIMA` e `PUREZA_MINIMA` (0,8 cada) decidem quando a tela crava o
"comece por" e o X. No teste cego eles se mostraram conservadores: quando a
tela não afirma, a 1ª hipótese ainda acerta em 81–97% dos casos.

| Limiares (explica, pureza) | Parcial: afirma / acerta | Parcial + 5: afirma / acerta | Duro: afirma / acerta |
|---|---|---|---|
| 0,8 / 0,8 (atual) | 62% / 85% | 29% / 92% | 10% / 94% |
| 0,7 / 0,7 | 75% / 87% | 46% / 91% | 17% / 92% |
| 0,6 / 0,6 | 90% / 87% | 66% / 91% | 28% / 87% |
| 0,5 / 0,5 | 97% / 88% | 84% / 91% | 41% / 85% |

O teste cego é otimista em um ponto: o rompimento sorteado segue a mesma árvore
de caminho mais curto que o algoritmo usa, e a fibra de verdade pode correr por
outro caminho. Por isso a decisão de afrouxar os limiares ficou com o Paulo, e
a calibração final é com os rompimentos registrados no campo "Onde rompeu"
(`manage.py aferir_rompimentos`).
