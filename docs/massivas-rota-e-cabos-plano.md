# Rota e cabos na tela de Massivas — plano

**Objetivo:** sair de "trecho suspeito, em texto" para "onde provavelmente rompeu,
desenhado no mapa, com os cabos candidatos nomeados".

**Decidido em 2026-09-18:** R1 respondida (o prefixo do nome da CTO é área, não
topologia) — R4 rebaixada e R5 bloqueada. Primeira tarefa a executar: **R7**.

**Regra que não muda (§2.3 do [massivas-plano.md](massivas-plano.md)):** a tela
pode mostrar *ligação lógica* e *candidato*, nunca *traçado de cabo* nem
"cabo X rompido". Uma linha no mapa que o técnico leia como o caminho real da
fibra, sem ser, é pior do que não desenhar nada — ele cava no lugar errado.

---

## 1. O que existe hoje (medido em produção, 2026-09-18)

### Planta cadastrada (`NetworkElement`)

| kind  | qtd   | geo | observação |
|-------|-------|-----|------------|
| POP   | 9     | 7 com lat/lon | nomes inconsistentes ("OLT VOTORANTIM" cadastrado como POP) |
| OLT   | 3     | **0 com lat/lon** | mas todas com `parent_kind=POP` → herda a coordenada do POP |
| PON   | 307   | —   | `parent_kind=OLT` |
| CTO   | 1.445 | 1.438 com lat/lon | **100% com `parent_kind=OLT`** |
| CABLE | 1.191 | **sem geometria** | só id, descrição e projeto |

### O vínculo que falta

- `df_elemento_coordenada` mapeia elemento → id de coordenada, mas **a tabela de
  coordenadas não tem endpoint na API** (§2.3). Sem isso, cabo não tem traçado.
- Não existe vínculo cabo↔CTO. O único campo comum é `project_external_id`, e
  ele é grosso demais: **562 cabos no projeto 1, 318 no 7, 258 no 5**. "Os cabos
  do projeto" seriam centenas de candidatos — ruído, não pista.
- CTO aponta para OLT, nunca para PON (§2.5c): a PON é propriedade do login.

### Dois achados novos que abrem caminho

**a) O nome do cabo carrega a classe do cabo.** Amostra real:

    CLIENTE DROP 1FO 30
    FIBRA AS80 12FO ATENDIMENTO 65
    FIBRA AS80 12FO BACKBONE 18
    FIBRA AS80 72FO 4 (extensão de rompimento)

Dá para separar **BACKBONE** (tronco) de **ATENDIMENTO** (distribuição) de
**DROP** (drop do cliente) e ler a capacidade (1/6/12/72 FO) direto da descrição.
Numa massiva, o que interessa é backbone e atendimento — drop de cliente nunca
explica 30 clientes fora. Isso já filtra o candidato sem inventar topologia.

**b) O prefixo do nome da CTO agrupa caixas fisicamente próximas.** Medido sobre
as 1.438 CTOs com coordenada: o prefixo (`A10`, `B47`, …) forma **106 grupos**
cobrindo 1.087 caixas, e o raio desses grupos é **mediano 240 m, p90 440 m, pior
caso 900 m**. Ou seja: a convenção de nomenclatura do cadastro **é** um proxy de
derivação física — caixas com o mesmo prefixo penduram no mesmo bolsão.

Isso não é traçado de cabo, mas é o suficiente para responder "o rompimento está
no bolsão A34" em vez de listar 12 caixas soltas. Precisa de uma confirmação de
quem cadastrou a planta antes de virar regra (tarefa R1).

### Onde isso aparece hoje

As duas massivas abertas agora em produção:

    PON 364 · 31 afetados · ALTA
      trecho capoavinha 4 → CTO 1287 VOTORANTIM, CTO 1346 - CAPOAVINHA, CTO 1347
    OLT 1 · 28 afetados · MÉDIA
      trecho A31 - SP 02 → A34 - SP 13, A34 - SP 14, A37 - SP 02 (+8 caixas)

O segundo caso mostra bem o problema: "A31 → A34, A34, A37 (+8 caixas)" é uma
lista, não uma direção. O técnico ainda precisa abrir o IXC e olhar o mapa pra
saber pra onde ir.

---

## 2. O que dá pra desenhar sem mentir

Três camadas, em ordem de honestidade decrescente. As duas primeiras entram; a
terceira só com dado novo.

1. **Ligação lógica CTO → POP** (linha pontilhada, cinza). A CTO tem coordenada e
   aponta pra OLT; a OLT aponta pro POP, que tem coordenada. É uma reta entre dois
   pontos que existem, rotulada como *ligação lógica, não o caminho da fibra*.
   Pontilhado é deliberado: linha cheia lê como traçado.
2. **Trecho suspeito entre CTOs** (linha laranja, pontilhada, com seta). Liga a
   CTO a montante às demais integralmente fora — é o `suspected_segment_label` de
   hoje, desenhado em vez de escrito.
3. **Traçado real do cabo** (linha cheia). Só existe se conseguirmos geometria —
   ver tarefa R2. Sem ela, não desenhamos.

---

## 2b. O que discrimina a causa (consultoria de domínio, 2026-09-18)

Levantado com um especialista em NOC/GPON. **É conhecimento de domínio de um
consultor, não fonte verificada** — o que está marcado como *medido* abaixo foi
conferido contra os nossos dados; o resto é hipótese a validar em campo.

**A regra que separa as duas causas mais confundidas:**

> Rompimento correlaciona com **topologia**. Falta de energia correlaciona com
> **geografia**.

Se o agrupamento GEO acende mas o agrupamento CTO/PON não, é quase certo que é
queda da concessionária, não cabo — clientes de PONs diferentes caem juntos
porque estão no mesmo alimentador da distribuidora. Isso muda a ação: não se
manda viatura. O detector já tem os dois escopos; o que falta é dizer isso na
tela.

**Os discriminadores, em ordem de valor:**

1. **Quem NÃO caiu.** Segundo o especialista, é o sinal mais informativo de
   todos, e hoje não aparece em lugar nenhum da tela. "CTO-14: 12/12 fora ·
   CTO-13, mesma PON: 0/9 fora" delimita o trecho melhor que qualquer mapa.
2. **dying-gasp vs LOS.** Já temos o dado (#148). Falta o veredito: >70%
   dying-gasp é energia, >70% LOS sem dying-gasp é fibra. Hoje a tela mostra a
   distribuição e deixa a conclusão por conta do leitor.
3. **Cronologia fina.** *Medido:* o `dropped_at` tem resolução de **segundo**,
   não de poll — nas duas massivas abertas agora, 31 quedas em 22 segundos
   distintos (spread 337 s) e 28 quedas em 24 segundos distintos (spread 182 s).
   O timestamp vem do `ultima_conexao_final` do IXC (`drop_tracking.py:154`), não
   do relógio do poll. **Ou seja: a cronologia fina já está no banco e ninguém
   usa.** Rompimento derruba todo mundo no mesmo segundo; energia com nobreak
   derruba escalonado. Os dois eventos de agora estão espalhados por minutos —
   o que, pela regra, não parece rompimento.
4. **Degradação óptica antes da queda.** RX caindo ao longo de dias na mesma PON
   = fibra sob estresse. Vale tela própria, preditiva, fora desta frente. O que
   discrimina não é o valor absoluto e sim o desvio em relação aos vizinhos da
   mesma CTO: um cliente a -28 com vizinhos a -21 é o drop dele; a CTO inteira
   caindo 5 dB junta é o trecho antes dela.
5. **Reincidência por trecho.** "Esta CTO/PON teve N massivas em 90 dias" acha
   trecho cronicamente ruim (travessia de rodovia, poste de esquina). Temos o
   histórico; falta o contador.

**Três coisas que faltam como objeto no sistema:**

- **Causa confirmada no fechamento.** Sem o NOC registrar o que era de verdade
  (rompimento / energia / OLT / manutenção / falso positivo), nunca saberemos se
  o veredito automático acerta. É o mesmo buraco de calibração que já apareceu em
  churn e em conversas ruins.
- **Manutenção programada.** Sem ela, toda janela vira "massiva", envenena a
  estatística e treina a equipe a ignorar alerta.
- **Cobertura do denominador.** "% da CTO afetada" só vale se soubermos quantos
  logins ativos a caixa tem. *Medido:* 3.270 de 8.293 conexões têm PON
  preenchida — a cobertura precisa aparecer na tela em vez de ser presumida.

---

## 3. Tarefas

### R1 — ~~Confirmar a convenção de nomenclatura~~ — RESPONDIDA (2026-09-18)
**`A34` é só área/região do projeto. Não representa caixa de emenda nem ponto de
derivação.**

Consequência: o agrupamento por prefixo — apesar do raio mediano de 240 m — é
**referência de lugar, não pista de topologia**. Duas CTOs com o mesmo prefixo
não compartilham necessariamente cabo. Isso derruba a premissa de R4 e R5, que
foram rebaixadas abaixo.

Ainda serve para uma coisa: dizer *onde* é ("massiva na área A34"), que é melhor
que enfileirar 12 nomes de caixa. Mas não serve para dizer *qual trecho* rompeu.

### R2 — Spike: existe alguma forma de obter geometria?
Três caminhos a testar, em ordem de custo:
- bater em `df_elemento_coordenada` e nas rotas vizinhas na API de produção e
  registrar a resposta real (hoje a afirmação "não tem endpoint" está no plano
  mas não tem evidência colada);
- verificar se o InMap exporta o projeto em KML/KMZ — se exportar, um importador
  manual (upload de arquivo) dá traçado de verdade, sem depender da API;
- verificar se o banco do IXC é acessível por outro meio que não a API.
**Entrega:** resposta sim/não por caminho, com o que foi tentado. Se algum der
certo, vira épico próprio (traçado real muda tudo).

### R3 — Desenhar as ligações lógicas no mapa — FEITA (2026-09-18)
Camadas 1 e 2 da seção 2, com legenda explícita e estilo pontilhado.
Inclui: seta apontando da CTO a montante para as demais; POP no mapa sempre que
houver massiva (hoje ele só aparece como ponto solto).
**Depende de:** nada. É a tarefa que dá resultado visível mais rápido.

**Como ficou.** Duas camadas de linha, ambas **tracejadas**: cinza da caixa até o
POP que a alimenta, laranja da caixa mais próxima do POP até as demais afetadas.
O sentido do trecho usa a mesma `haversine_meters` do detector (agora pública) —
se a tela desenhasse a seta para um lado e o rótulo do trecho dissesse outro, uma
das duas estaria mentindo. Sem POP no cadastro não se desenha nada: chutar o
sentido seria pior.

O tracejado é construído na mão, quebrando cada segmento em pedaços separados
por `None`, porque **o Plotly não tem `dash` em traço de mapa**. Linha cheia aqui
leria como traçado de cabo, e o técnico cavaria onde a linha passa.

Junto entrou a parte do mapa que faltava em **R6**: as caixas vizinhas intactas
como ponto verde-escuro.

**Dois erros que só a imagem pegou:**
1. Zoom fixo 12 espremia o evento num canto e as ligações, curtas, sumiam. O
   enquadramento agora sai do conteúdo (bounding box dos pontos).
2. A primeira versão do cálculo de zoom cortou o POP para fora do quadro: o
   MapLibre serve tile de **512 px**, não os 256 px do Web Mercator clássico, e
   errar isso custa exatamente um nível de zoom.

### R4 — Resumir o trecho por área (rebaixada)
A ideia original era agrupar por derivação física. **R1 mostrou que o prefixo não
é derivação**, então isto vira só melhoria de legibilidade do rótulo: trocar a
lista de 12 caixas por "área A34 (2 caixas), A37 (1 caixa), a jusante de A31" —
com o cuidado de não sugerir que a área é o trecho.
**Prioridade baixa.** Cosmético, não diagnóstico.

### R5 — Cabos candidatos (BLOQUEADA)
A ideia era listar os cabos do projeto filtrados por classe (BACKBONE e
ATENDIMENTO; DROP nunca). **Sem R1, não há como estreitar a lista**: o projeto 1
tem 562 cabos e nenhum campo os liga a uma CTO. Um card com 562 candidatos não é
pista, é ruído — e dá ao técnico a sensação falsa de que o sistema sabe algo.

**Só destrava se R2 achar geometria.** Até lá, não implementar.

### R6 — "Quem não caiu" no card e no mapa — FEITA (2026-09-18)
Para cada massiva: irmãos do mesmo elemento que continuam de pé, com
caídos/total ("CTO-14: 12/12 fora · CTO-13, mesma PON: 0/9 fora"). No mapa, as
CTOs vizinhas online entram como pontos vazados. Quem ficou de pé delimita o
trecho tão bem quanto quem caiu, e hoje isso não aparece em lugar nenhum.
**Depende de:** nada.

**Como ficou.** A vizinhança sai da **PON**, não da OLT: a PON é propriedade do
login (§2.5c), então as portas da massiva vêm dos logins que caíram e as caixas
irmãs são as que têm login nessas mesmas portas. Quando os afetados não têm PON
no cadastro, o bloco declara que não dá para determinar — cair para "mesma OLT"
traria centenas de caixas sem relação com o trecho, o que é pior do que não
responder.

A caixa **intacta** encabeça a lista: é ela que marca o limite. E intacta exige
denominador > 0 — uma caixa cujo único login é de contrato cancelado tem zero
ativos, e oferecê-la como "0/0 fora, intacta" seria apontar como limite do trecho
um lugar onde não há ninguém para cair. O denominador é o mesmo do detector
(`active_logins_per_cto`, agora pública pelo mesmo motivo: duas contagens que
deveriam ser iguais divergem no dia em que só uma for corrigida).

A parte do mapa (caixas vizinhas intactas como ponto) entrou junto com R3.

### R7 — Veredito de causa provável no card da massiva — FEITA (2026-09-18)
Uma linha, no topo do card: "provável **energia** — 78% dying-gasp, quedas
espalhadas por 5 min" ou "provável **fibra** — 91% LOS, todas no mesmo segundo".
Combina dying-gasp/LOS (já temos), spread cronológico (já temos, não usado) e
topologia vs geografia (já temos os dois escopos). Declara a cobertura e nunca
afirma sem base — igual ao resto da tela.
**Depende de:** nada. Maior retorno por esforço desta lista.

**Como ficou, e o que a calibração mudou.** O veredito saiu com três estados
nomeados (energia / fibra / misto) mais a recusa explícita de opinar quando a
base não dá: menos de 5 quedas ou cobertura de causa abaixo de 40%. O limiar
para nomear a causa é 70% de concentração; entre os dois, "sinais misturados",
que é resposta legítima.

O escopo entra como segundo sinal, e o caso mais útil é o de **contradição**:
dying-gasp num escopo topológico (PON/OLT/CTO) ganha a ressalva de que, se fosse
a concessionária, esperaria-se gente de outras PONs junto.

**A cronologia NÃO virou critério, e isso contraria a consultoria.** Medição de
2026-09-18 sobre as 24 massivas: o evento com maior proporção de LOS é o de
*maior* espalhamento (337 s), e massivas de dying-gasp puro fecham em 20 s —
exatamente o oposto do que a regra "rompimento derruba todo mundo no mesmo
segundo" previa. O `ultima_conexao_final` do IXC parece registrar quando a OLT
reportou, não quando o cliente caiu. Então o espalhamento aparece na tela como
descrição factual e a leitura fica com quem opera.

**Achado que salvou o critério de um erro:** na partida a frio de 09/09, 189
quedas ficaram com o mesmo timestamp **ao microssegundo** — era o relógio do
poll, não o IXC. Lido como cronologia, seria "todas no mesmo instante", ou seja,
simultaneidade inventada a partir de ausência de dado. Horário vindo do IXC tem
sempre microssegundo zero, e é assim que os sintéticos são excluídos da conta.
No geral a cobertura é boa: **2.534 de 2.787 quedas (91%)** têm horário real, e
as massivas recentes têm 100%.

### R8 — Reincidência do trecho — FEITA (2026-09-18)
Contador "N massivas nos últimos 90 dias" por elemento, no card e no histórico.
**Depende de:** nada.

**Como ficou, e por que a janela mudou de nome.** O bloco sai como ordinal — "3ª
massiva deste elemento" — e não como contagem solta: "3 massivas" ainda deixa o
leitor perguntando se a de hoje está dentro. Reincidente ganha cor e a data da
anterior; primeira ocorrência sai cinza, dizendo que é a primeira.

**Os 90 dias viraram texto variável.** *Medido em produção:* a base tem **24
massivas e o registro começa em 09/09** — 9 dias. Um rótulo "1 massiva nos
últimos 90 dias" sobre 9 dias de histórico é elogio falso a um trecho que
ninguém observou, e o erro é exatamente o que esta tela existe para evitar. A
janela efetiva passou a ser a menor entre os 90 dias e o registro que existe, e
ela vai escrita ao lado do número ("em 9 dias de registro") com a ressalva de que
o contador não enxerga antes disso. Quando o histórico passar dos 90 dias, a
frase volta sozinha a dizer 90 dias.

**Escopo sem elemento não conta.** *Medido:* **16 das 24 massivas são GEO**, e
GEO não tem `element_external_id` — o cluster de proximidade muda de forma a cada
evento. Somar todas num contador só diria "16 massivas neste trecho" juntando
bairros diferentes. Sem elemento, o bloco se recusa e explica por quê; a
identidade usada é `(scope, element_external_id)`, nunca o `element_label` (texto
de tela, muda quando o cadastro é corrigido) nem o `suspected_segment_label` (que
depende de quem caiu naquele evento, e seria diferente a cada ocorrência do mesmo
trecho).

**Detalhe que só aparece no histórico:** o ordinal conta apenas o que veio
*antes* daquela massiva. Uma encerrada aberta hoje não pode virar "a 3ª" por
causa do que aconteceu depois dela — o card diria algo que não era verdade
enquanto o evento acontecia.

A distribuição atual, para calibrar expectativa: 4 massivas na OLT 1, 3 na OLT 2,
1 na PON 364 — 3 de 4 elementos distintos já são reincidentes em 9 dias.

### R9 — Causa confirmada no fechamento da massiva
Campo simples (rompimento / energia / OLT-equipamento / manutenção / falso
positivo) preenchido quando o evento encerra. Sem isso não há como calibrar R7.
**Nota:** é o primeiro campo de *entrada* de dados desta ferramenta — até aqui o
dashboard é read-only (ver memória do projeto). Decisão de produto antes de
código.

### R10 — Manutenção programada como objeto
Janela com escopo e horário; evento que cai dentro dela nasce marcado como
esperado e não alarma.
**Depende de:** R9 (mesmo modelo de dados).

---

## 4. Achado lateral a investigar

`affected_fraction = 1.55` na massiva PON 364 aberta agora — fração de 155%, o
que não deveria existir. Ou o denominador (logins ativos da PON) está menor que o
número de afetados, ou o campo está sendo gravado em escala diferente. Não é
desta frente, mas contamina a confiança do detector: abrir separado.
