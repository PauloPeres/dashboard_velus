# Geometria de cabo — plano do épico

**De onde vem:** o spike R2 (2026-09-19, em
[massivas-rota-e-cabos-plano.md](massivas-rota-e-cabos-plano.md)) derrubou a
premissa de que a API do IXC não expõe coordenadas. Ela expõe, e completa:

| Recurso | Volume | O que traz |
|---|---|---|
| `df_elemento` (`tipo=CB`) | 1.191 | o cabo: descrição, projeto, tipo |
| `df_elemento_coordenada` | 12.922 | elemento → coordenada, com `sequencia` (ordem do vértice) |
| `df_coordenada` | 10.520 | latitude e longitude |

1.189 dos 1.191 cabos têm 2 ou mais vértices (mediana 5, máximo 121). E o
vínculo cabo↔CTO, que não existe por campo, sai por geometria: **1.120 das 1.431
CTOs com coordenada estão a ≤10 m de um vértice de cabo, mediana 0,0 m** — o
InMap usa o mesmo ponto da caixa.

**O que este épico entrega:** o traçado real no nosso banco, a camada de cabo no
mapa da massiva e o card de cabos candidatos (R5), que estava bloqueado.

---

## 1. Regras que não mudam

- **Desenhar o cabo é leitura de cadastro; dizer que ele rompeu é inferência.**
  A linha cheia no mapa passa a ser legítima porque agora é traçado de verdade —
  mas o rótulo continua "cabo candidato", nunca "cabo rompido". Confirmação de
  causa é o R9, que não existe.
- **Cobertura vai na tela.** ~15% das caixas não têm cabo cadastrado a menos de
  30 m. Onde não há cabo perto, a resposta é dizer isso, não esticar o raio até
  achar um.
- **Sync, não consulta ao vivo.** São ~25 mil registros em três recursos; puxar
  isso a cada abertura de tela seria trocar uma tela rápida por uma lenta e
  ainda pendurar o IXC.
- **Drop nunca é candidato.** A classe sai do nome (`DROP`, `ATENDIMENTO`,
  `BACKBONE`) — drop de cliente não explica 30 clientes fora.

## 2. Tarefas

### G1 — Modelo de geometria — FEITA (2026-09-19)
`NetworkElementGeometry`: elemento (external_id + kind), lista ordenada de
pontos, projeto, carimbo de atualização. Os pontos ficam em JSON, não em tabela
de vértice: a geometria é lida inteira e nunca consultada por vértice, e 12.922
linhas viram 1.191 documentos.

**Como ficou.** Model separado do `NetworkElement`, com a mesma chave composta
`(organization, source_type, kind, external_id)` — é o mesmo objeto visto de
outro ângulo. Separado porque juntar faria toda query de planta carregar
polilinha, e a planta é lida o tempo todo pelo detector. `points` é
`[[lat, lon], ...]` **na ordem do traçado**; a ordem é o dado.

### G2 — DTO, port e adapter — FEITA (2026-09-19)
`ElementGeometryDTO` no domínio; o `NetworkElementSourcePort` ganha o método de
geometria; `IxcNetworkElementSource` monta a polilinha juntando os três recursos
em memória (uma passada, três paginações), ordenando por `sequencia`.

**Quirk de API:** o filtro tem que usar `df_elemento_coordenada.id_elemento`.
Pedir por `id_df_elemento` devolve a página HTML de erro do IXC.

**Como ficou, e o que o teste pegou.** Os vínculos voltam da API **fora de
ordem** — no spike, `sequencia` 1, 2, 0 na mesma página, porque a listagem sai
por id desc. Montar na ordem recebida desenharia um cabo em zigue-zague, e é
exatamente esse o teste que trava o comportamento. Coordenada ilegível ou
`(0, 0)` é descartada sem levar o cabo junto: `(0, 0)` cairia no golfo da Guiné.
Cabo sem nenhum ponto **não vira DTO** — o `ElementGeometryDTO` recusa lista
vazia no `__post_init__`, porque traçado vazio faria a tela desenhar nada
achando que desenhou algo.

### G3 — Repositório e sync — FEITA (2026-09-19)
Upsert por `(organization, source_type, external_id)`, e a geometria entra na
mesma rodada do sync de planta que já existe. Cabo que perde vértice no cadastro
perde vértice aqui — o traçado não é histórico.

**Como ficou, e a decisão que evitou uma capability.** O dispatch do sync é um
repositório por capability, e a planta agora emite dois DTOs. A saída óbvia
seria uma capability `NETWORK_GEOMETRY` — e ela custaria outra linha de
credencial em `OrganizationDataSource` e outro agendamento **para ler os mesmos
endpoints do mesmo sistema na mesma frequência**. Em vez disso, um
`_PlantRepository` roteia o DTO pelo tipo, e a geometria viaja na rodada diária
da planta. Adapter que não conhece geometria (de terceiro, antigo) simplesmente
não tem o método, e a planta sincroniza como sempre.

### G4 — Cabos candidatos no card (R5) — FEITA (2026-09-19)
Para cada massiva: os cabos cujo traçado passa a ≤30 m de alguma caixa afetada,
excluindo DROP, com a distância e a classe. Declara quantas caixas do evento
ficaram sem cabo por perto.

**Como ficou.** A distância é medida contra o **segmento**, não contra o
vértice: cabo de dois vértices a 200 m passa rente a uma caixa no meio do
caminho, e medir só vértice diria 100 m — o cabo que passa na porta da caixa
sairia da lista. A ordenação é por **quantas caixas o cabo toca**, depois por
distância: o cabo que costura cinco das seis caixas fora é a pista, mesmo que
outro encoste mais perto de uma delas.

Três estados distintos, e nenhum deles é "nenhum candidato": sem caixa com
coordenada (não há de onde medir), sem traçado sincronizado (falta rodar o
sync), e nenhum cabo no raio (o projeto não cobre aquele trecho — que é
resposta, não falha).

O raio de 30 m sai da medição, não de gosto: a ≤30 m estão 1.211 das 1.431
caixas, e esticar até o p90 (81,5 m) traria, justamente para as caixas mal
cadastradas, o cabo de outra rua.

### G5 — Camada 3 do mapa: o traçado real — FEITA (2026-09-19)
Linha **cheia** (a primeira legítima nesta tela), só dos cabos candidatos — o
mapa com 1.191 cabos seria um borrão. Legenda separa das ligações tracejadas,
que continuam sendo inferência.

**Como ficou.** Roxa, cheia, por baixo de todo o resto — como no papel. Cada
cabo é quebrado com `None` entre os traçados, senão o último ponto de um cabo
ligaria no primeiro do próximo e desenharia um cabo que não existe. A legenda
passou a explicar o **estilo**: tracejado é inferência de cadastro, cheio é
traçado do projeto, e mesmo o cheio diz por onde o projeto passa a fibra, nunca
onde ela rompeu.

O teste que travava "Nenhum cabo é desenhado" foi reescrito — a regra que
sobrevive não é "não desenhe cabo", é "não diga que ele rompeu".

### G6 — Testes e verificação em produção — FEITA (2026-09-19)
Domínio e adapter com teste próprio; o card e o mapa com teste de tela. No fim,
rodar o sync em produção e conferir a tela de uma massiva real.

- `tests/test_network_geometry.py` — distância até o segmento, classe do cabo,
  ordenação dos candidatos, drop fora, e a contagem de caixas sem cabo;
- `tests/test_ixc_geometry_adapter.py` — a ordem da `sequencia`, cabo sem ponto,
  coordenada quebrada e `(0, 0)`;
- `tests/test_massivas_dashboard.py` — os três estados do card, o traçado no
  mapa e a ausência da palavra "rompido" na tela.

**Verificação em produção (2026-09-19).** O sync trouxe **1.191 traçados**,
1.189 com 2 ou mais vértices, mediana de 5 e máximo de 121 — exatamente o que o
spike tinha previsto. A tela geral monta em 1,4 s com duas massivas abertas; o
detalhe, em 0,6 s.

As duas massivas abertas no momento da verificação:

| Massiva | Caixas | Candidatos | Caixas sem cabo a 30 m | Melhor candidato |
|---|---|---|---|---|
| PON 364 | 8 | 5 | 1 | "12fo", 5 caixas, 0,0 m |
| OLT 1 | 15 | 24 | 0 | atendimento, 2 caixas, 0,0 m |

A distância 0,0 m em praticamente todos confirma o que o spike mediu: o InMap usa
o mesmo ponto da caixa. E o caso da PON 364 é o que a feature existe para
produzir — **um cabo que costura 5 das 8 caixas fora**, enquanto os outros
encostam em uma só.

**O que a verificação derrubou:** a premissa §1a de que "o nome do cabo carrega a
classe". Ela vale para **47%** do cadastro: 431 ATENDIMENTO, 60 BACKBONE, 67
DROP — e 633 sem classe nenhuma ("01FO", "rede neutra", "FIBRA AS80 24FO 3",
"link transporte interligação com Sorocaba").

### G7 — A classe vem do tipo, não do nome — FEITA (2026-09-19)

**Pergunta do Paulo, ao ler o resultado acima: "a API do IXC não tem parâmetro de
que cabo é?"** Tem. O `df_elemento` traz `id_tipo_elemento`, e o catálogo
`df_tipo_elemento` (174 registros) traz o `nome_tipo`. Eu tinha lido a classe da
descrição do cabo e ignorado o campo que responde a pergunta.

Lendo o tipo, os 1.191 cabos são:

| Classe | Cabos | Tipos |
|---|---|---|
| ATENDIMENTO | 843 | `FIBRA AS80 06 FO ATENDIMENTO`, `FIBRA AS80 12FO ATENDIMENTO`, `4FO ATENDIMENTO` |
| DROP | 125 | `CLIENTE DROP 1FO`, `CLIENTE DROP 2FO` |
| BACKBONE | 124 | `FIBRA AS80 12FO BACKBONE`, `FIBRA AS80 06FO BACKBONE` |
| sem classe | 99 | `FIBRA AS80 24FO`, `36FO`, `48FO`, `72FO`, `144FO`, e 11 sem tipo |

Os 53% sem classe caem para **8%**.

**E era um defeito, não só cobertura:** **57 cabos cujo tipo é `CLIENTE DROP
1FO` se chamam apenas "01FO"**. Lidos pelo nome, entravam na lista de candidatos
a explicar uma massiva de trinta clientes — que é exatamente o que um drop de um
filamento não pode fazer. Foram para produção assim e ficaram lá algumas horas.

A classe passou a sair do tipo, com o nome como reserva para os 11 cabos sem
tipo cadastrado. O `NetworkElementGeometry` ganhou `type_name` (migration 0009) e
o adapter passou a ler o catálogo junto com o resto.

**O que continua sendo recusa deliberada:** `FIBRA AS80 24FO` é quase certamente
tronco pela capacidade, mas o tipo não diz "BACKBONE". Deduzir classe da
capacidade seria inferir onde o cadastro cala, então esses 8% seguem declarados
como sem classe na tela.

**Um campo que não serve:** `cabo_numero_fibras` é inconsistente — o tipo
"FIBRA AS80 24FO" declara 6 fibras e o "144FO" declara 12. Parece ser fibras por
tubo. Não use para capacidade.
