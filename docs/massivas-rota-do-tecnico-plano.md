# A rota do técnico: da primeira CEO afetada até o rompimento

**Feedback de um técnico, trazido pelo Paulo em 21/09/2026**, olhando
`/operations/massivas/141/#mapa`. Sete pedidos, e todos são a mesma pergunta
dita de sete jeitos: *por onde eu começo?*

> 1. saber, das caixas de emenda (CEO), quais CTOs caíram e quais não;
> 2. identificar a **primeira CEO afetada**, seguindo a rota do cabo que sai do POP;
> 3. essa CEO é o **ponto de partida** do técnico;
> 4. no mapa **e no texto**: qual a primeira CEO e a primeira CTO afetadas nessa rota, para ele baixar essa CEO do poste;
> 5. considerar os **drops**, porque o projeto tem drop ligando caixa a caixa;
> 6. o cabo tem **direção** no projeto do IXC — uma setinha no desenho ajudaria;
> 7. se der para **inferir onde está o rompimento**, marcar um "X" ali.

E mais dois, que chegaram em seguida:

> 8. ter o botão de **copiar a mensagem para o técnico dentro da massiva** também, e não só na lista;
> 9. **puxar uma caixa (CTO e CEO) anterior ao rompimento**, para o caminho ficar claro.

O 8 já está feito — era pequeno e independente do resto (ver o fim deste
documento). O 9 virou o C9.

O pedido é bom e a tela hoje não responde nada disso: ela mostra pontos e o
traçado dos cabos candidatos, sem dizer o que vem antes do quê.

---

## O que foi medido antes de desenhar (produção, 21/09/2026)

Nada aqui é suposição de projeto: são contagens na base.

**A planta que temos**

| | |
|---|---|
| POPs | 9 (7 com coordenada) |
| OLTs / PONs | 3 / 307 |
| CTOs (cadastro FTTH `rad_caixa_ftth`) | 1.445 |
| CEOs (projeto, `df_elemento` tipo CA) | 317, todas com ponto |
| Cabos (projeto, tipo CB) | 1.191, sendo 1.189 com 2+ vértices |

O projeto do IXC tem ainda 90 postes (PT), 72 splitters (SP) e 51 áreas (AR),
que **não sincronizamos** — e não há elemento "CTO" no projeto: a caixa do
cliente no desenho é a *Caixa de Emenda* (286 delas são "Caixa de Emenda
Vermelha"). Ou seja, **a CTO do cadastro e o desenho do projeto são dois mundos
que só se encontram por coordenada.**

**Como a CTO encosta no desenho** — o número que mudou o plano:

| distância da CTO ao… | p50 | até 5 m | até 15 m | até 150 m |
|---|---|---|---|---|
| vértice de cabo mais próximo | **0 m** | **72%** | 79% | 91% |
| CEO mais próxima | 132 m | 0% | 3% | 56% |

A CTO está **em cima do cabo**, e quase nunca na ponta dele: o cabo passa por
dentro da caixa e segue. Foi isso que derrubou a primeira tentativa de montar o
grafo ligando só as **pontas** dos cabos — ela alcançou 428 das 1.438 CTOs
(30%). Cortando o cabo em **todo elemento que fica sobre o traçado**, o mesmo
código alcança **949 CTOs (66%) e 215 CEOs (68%)** a partir de um POP.

**As pontas dos cabos** (amostra de 300): CEO→CTO 98, CEO→CEO 68, CTO→CTO 60,
sem elemento identificável 28, CTO→CEO 25. Dos **125 cabos classe DROP**, 65 vão
CEO→CTO — o drop é a última perna, e entra no grafo como qualquer outra aresta.

**A direção do traçado não serve como está.** Dos 1.189 cabos, **654 começam**
mais perto do POP e **513 terminam** mais perto: a ordem dos vértices no IXC não
é sistematicamente POP→cliente. A seta do item 6 **não pode sair da sequência do
desenho** — ela tem de sair da direção inferida pelo grafo (quem está mais perto
do POP, seguindo cabo).

**A massiva 141**, que o técnico estava olhando: escopo GEO (cluster
geográfico, sem elemento de rede), 11 clientes fora, 4 CTOs — 1234, 1267, 1302,
1361. Nenhuma das quatro é alcançável a partir de um POP no grafo de hoje: a
CTO 1361 está a 0 m de um cabo, mas o pedaço de planta onde ela vive **não se
liga a nenhum POP**. Só **3 dos 7 POPs** têm cabo encostando neles.

---

## O que dá para fazer hoje, e o que não dá

| pedido | dá? | com que cobertura |
|---|---|---|
| 1. CTOs que caíram / não caíram por CEO | **sim** | 66% das CTOs entram no grafo; o resto some da conta e a tela declara |
| 2. primeira CEO afetada na rota do POP | **parcial** | só onde a rota chega a um POP — 3 dos 7 POPs estão ligados ao desenho |
| 3. ponto de partida do técnico | **parcial** | idem; sem POP, dá para dar o "ancestral comum" das CTOs afetadas, que já é a caixa a baixar |
| 4. no mapa e no texto | **sim** | depende do 2/3 |
| 5. drops no grafo | **sim** | 125 cabos DROP entram como aresta, marcados como última perna |
| 6. setinha de direção | **sim, mas não do IXC** | a direção vem do grafo (distância de cabo até o POP), não da ordem dos vértices |
| 7. "X" no provável rompimento | **sim, como inferência** | entre a última caixa sã e a primeira afetada na rota |

---

## Plano

### C1 — Grafo da planta (a base de tudo)

Um serviço de domínio que monta, a partir do que já sincronizamos:

- **nós**: POP, CEO (projeto) e CTO (cadastro FTTH);
- **arestas**: cada cabo é **cortado** em todo elemento que fica a ≤ 15 m de um
  de seus vértices, na ordem do traçado; entre dois elementos consecutivos nasce
  uma aresta com o comprimento real do pedaço de cabo (não a reta).

Tolerância de 15 m medida, não escolhida no olho: 72% das CTOs estão a menos de
5 m e 79% a menos de 15 m; subir para 60 m acrescenta 4 pontos de cobertura e
começa a colar caixa em cabo de outra rua.

*Pronto quando*: o grafo tem ≥ 1.000 CTOs e ≥ 200 CEOs ligadas, e um teste trava
o caso "cabo passa por dentro da caixa" com uma planta de brinquedo.

### C2 — Direção: distância de cabo até o POP

Dijkstra a partir de todos os POPs, com o peso em metros de cabo. Dá, para cada
elemento, **a que distância do POP ele está** e **por qual caminho** — e é isso
que define "antes" e "depois" no resto do plano.

*Ressalva que vai para a tela*: só 3 dos 7 POPs encostam no desenho. Onde a rota
não chega a um POP, o sistema **não inventa origem** — cai no C3b.

### C3 — A primeira CEO afetada e a primeira CTO afetada

Com as CTOs afetadas pela massiva:

- **C3a (com POP)**: ordenar os elementos das rotas por distância do POP; a
  primeira CEO que aparece em **todas** as rotas das CTOs afetadas é o ponto de
  partida. É a caixa a baixar do poste.
- **C3b (sem POP)**: o **ancestral comum** das CTOs afetadas dentro do
  componente — a caixa mais "acima" que serve todas elas. Sem POP não dá para
  dizer que é a primeira da rota, então a tela diz "caixa comum a todas as CTOs
  afetadas", que é verdade, em vez de "primeira CEO", que seria chute.

### C4 — Quais CTOs de cada CEO caíram, e quais não

Para cada CEO envolvida: a lista das CTOs que pendem dela no grafo, marcadas
**caiu / não caiu**. É o pedido 1, e é também o que dá confiança ao pedido 3 —
uma CEO com 100% das CTOs abaixo dela fora é um ponto de partida muito melhor do
que uma com 2 de 9.

### C5 — O "X" do rompimento provável

Na rota, o rompimento está **entre a última caixa que continua no ar e a primeira
que caiu**. O "X" vai no meio desse pedaço de cabo, sobre o traçado — e a tela
diz, ao lado, que é **inferência de topologia, não leitura**: ninguém mediu a
fibra ali.

Quando não há caixa sã antes (todas caíram até o POP), não existe "entre": o X
não aparece. Melhor sem X do que um X no lugar errado — ele manda um carro.

### C6 — O mapa

- **ícone e cor próprios para a CEO de partida** (hoje as emendas são todas
  iguais) e para a primeira CTO afetada;
- **setas** ao longo do traçado, no sentido POP→cliente **inferido pelo C2**;
- **"X"** do C5;
- as CTOs não afetadas da mesma CEO aparecem em cor de "no ar", que é o que
  delimita o trecho.

### C7 — O texto do técnico

A mensagem de copiar ganha, no topo, o que o técnico lê primeiro:

```
COMECE POR: CEO Caixa de Emenda Vermelha 138 (rua X, nº Y)
  ↳ 1ª CTO afetada na rota: CTO 1361 — 12 clientes fora
  ↳ desta CEO dependem 4 CTOs: 3 fora, 1 no ar
  ↳ provável rompimento: entre CEO 138 e CTO 1361, ~180 m de cabo
  ↳ cabo: FIBRA AS80 12FO BACKBONE 18
```

Com as coordenadas da CEO (que é onde o carro para) em vez das do cliente.

### C8 — Cobertura declarada

Toda tela que usar o grafo diz, em uma linha, **quantas das CTOs afetadas
entraram nele**. "3 das 4 CTOs desta massiva estão no desenho do projeto; a
quarta não aparece na rota" é informação; omitir seria deixar o técnico achar que
viu o mapa inteiro.

### C9 — Uma caixa antes do rompimento, para o caminho ficar claro

Pedido 9, que chegou depois: o mapa não mostra só o que caiu. Ele mostra
**também a caixa imediatamente anterior** ao provável rompimento — a última CEO
e a última CTO que continuam no ar naquela rota.

É o que transforma um ponto num caminho: com a caixa de trás no desenho, o
técnico vê de onde a fibra vem e para onde ela ia, e o "X" do C5 passa a ter as
duas pontas visíveis em vez de flutuar sobre o traçado. Ela entra em cor de "no
ar", como as vizinhas intactas da R6 já fazem hoje — o mesmo vocabulário visual,
porque é a mesma afirmação: *esta aqui não caiu, e é ela que delimita o trecho*.

Sem POP na rota (C3b), a "anterior" é a caixa do lado da caixa comum que
continua no ar; se não houver nenhuma, a tela diz isso em vez de desenhar uma
seta partindo do nada.

---

## O que precisa de decisão ou de dado novo

1. **Quatro POPs não encostam no desenho.** Ou a coordenada do POP está no
   prédio e a fibra sai de uma caixa na calçada, ou o projeto daquela região não
   foi desenhado. Isso limita o C2 e é a maior alavanca do plano — vale
   perguntar ao pessoal do InMap.
2. **A massiva 141 não é um bom caso de teste**, e isso não é defeito dela: é
   cluster geográfico, sem elemento de rede, e as 4 CTOs estão num pedaço de
   planta sem ligação com POP. O plano precisa de uma massiva com escopo de CTO
   ou PON para validar a rota ponta a ponta.
3. **Postes e splitters (162 elementos) não são sincronizados.** O splitter
   importa: é ele que diz quantos clientes pendem de uma caixa. Fica fora deste
   plano, anotado.
4. **Existe no IXC uma ligação de verdade entre elementos?** Tudo aqui é
   inferência por coordenada. Se o IXC guardar a fusão (que fibra de que cabo
   entra em que caixa), o grafo deixa de ser inferência e vira leitura — e o
   plano inteiro fica mais barato e mais honesto. Vale uma pergunta ao suporte
   antes de começar o C1.

---

## Feito fora do plano

**Pedido 8 — botão de copiar dentro da massiva** (21/09/2026). Quem abre o
detalhe para entender o evento é justamente quem vai mandar alguém para a rua;
voltar à lista só para copiar é a ida e volta que faz a pessoa desistir e
escrever o texto na mão — e o texto na mão perde as ressalvas que a mensagem
carrega ("trecho suspeito", "cabo candidato").

O comportamento do botão virou um parcial só (`_massivas_copiar_js.html`), usado
pelas duas telas: um segundo script divergiria no dia em que só um fosse
corrigido.
