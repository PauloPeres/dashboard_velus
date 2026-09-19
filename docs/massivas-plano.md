# Quedas & Massivas — plano de implementação

> Contrato compartilhado entre as tarefas #142–#147. Toda sessão que pegar uma
> dessas issues **lê este documento primeiro** e o `AGENT.md` (DDD, ports &
> adapters, ACL Pydantic obrigatória).

## 1. Problema

Quando cai um trecho da rede, a equipe descobre pelo telefone tocando. Não existe
tela que responda, ao vivo: **quantos caíram, onde, qual elemento provavelmente
rompeu, quem são os clientes e quem já voltou.**

Esta é a primeira ferramenta *operacional* do dashboard — as outras abas são
leitura estratégica. A diferença tem consequência de arquitetura: cadência de
sync em minutos, não em horas, e um fluxo de eventos em vez de estado corrente.

**A ferramenta é de tempo real e não tem pergunta sobre o passado** (decisão do
Paulo, 2026-09-08). Ela serve pra direcionar técnico durante uma massiva
acontecendo e ver cliente voltando ao vivo. O único histórico que sobrevive é o
registro das massivas em si — quando, quantos clientes, onde, de forma genérica.
Disso decorrem duas regras que valem mais que qualquer requisito de tela:

- **Nada de backfill.** As colunas novas nascem vazias e o sync normal as
  preenche na primeira rodada. Não se vai atrás de `raw_extras` antigo.
- **Partida a frio** (§5.7): a primeira observação de um login é linha de base,
  nunca queda. Sem isso, a estreia da ferramenta inventaria massivas de agosto.

## 2. O que a API do IXC realmente entrega

Verificado contra a API de produção em 2026-09-08. **Nada aqui é suposição.**

### 2.1 `radusuarios` — já sincronizado como `Connection`

O registro do login carrega, além do que já mapeamos:

| Campo IXC | Significado | Uso |
|---|---|---|
| `ultima_conexao_final` | **timestamp exato da queda** | eixo do tempo do detector |
| `ultima_conexao_inicial` | início da sessão atual | retorno do cliente |
| `motivo_desconexao` | causa RADIUS | painel de motivos (ver §2.4) |
| `id_caixa_ftth` | CTO do cliente | agrupamento por caixa |
| `ftth_porta` | porta na CTO | detalhe |
| `id_transmissor` | OLT | agrupamento por OLT |
| `id_concentrador` | BRAS/NAS | descarta falha de concentrador |
| `latitude` / `longitude` | ponto do cliente | mapa e cluster geográfico |
| `id_df_projeto` | projeto InMap | recorte |

Todos já chegam em `Connection.raw_extras` (o schema usa `extra="allow"` e
`get_extras()` devolve tudo) — **ninguém lê**. A tarefa #143 promove os que
importam para colunas, conforme o princípio 2 do `AGENT.md` §1.6 ("quando um
campo vira essencial, promove pro DTO").

Medição de hoje: **237 logins ativos offline, 45 deles com queda na mesma hora**,
193/237 com CTO e 231/237 com coordenada. O detector tem base real.

### 2.2 Topologia

| Endpoint | O que é | Volume | Geo |
|---|---|---|---|
| `rad_caixa_ftth` | CTO / caixa de atendimento | 1.445 | lat/lon, endereço, capacidade, `id_transmissor` |
| `radpop` | POP / ponto de transmissão | 9 | lat/lon |
| `radpop_radio_porta_fibra` | porta PON | 307 | via slot → transmissor |
| `df_elemento` (`tipo=CB`) | cabo | 1.191 | traçado via `df_elemento_coordenada` + `df_coordenada` (§2.3) |

Hierarquia utilizável: `POP → transmissor (OLT) → slot → porta PON → CTO → porta → login`.

### 2.3 ~~Limite: geometria de cabo é inacessível~~ — PREMISSA DERRUBADA (2026-09-19)

**O que se afirmava aqui:** `df_elemento_coordenada` só mapeia
`id_elemento → id_coordenada` e a tabela de coordenadas não tem endpoint na API.
**Isso era falso, e nunca tinha sido testado** — a afirmação entrou no plano sem
resposta crua colada ao lado. O spike R2 bateu na API de produção:

- **`df_coordenada` existe e devolve `latitude`/`longitude`** — 10.520 registros;
- `df_elemento_coordenada` traz `sequencia`, que é a **ordem dos vértices** —
  12.922 vínculos;
- **1.189 dos 1.191 cabos têm 2 ou mais vértices** (mediana 5, máximo 121).
  Nenhum cabo está sem ponto.

Ou seja: `df_elemento (CB) → df_elemento_coordenada (ordenado por sequencia) →
df_coordenada` **é o traçado real do cabo**, e ele está disponível.

O casamento cabo↔CTO, que também se dava por impossível, sai por geometria:
**1.120 das 1.431 CTOs com coordenada estão a ≤10 m de um vértice de cabo** (a
mediana da distância é 0,0 m — o InMap usa o mesmo ponto), 1.211 a ≤30 m, p90 de
81,5 m. Sobram ~15% de caixas longe de qualquer cabo cadastrado, e é essa a
cobertura que qualquer tela de cabo terá de declarar.

**O que continua valendo:** a regra de não dizer "cabo X rompido" não vinha só da
falta de traçado — vinha de não haver confirmação de causa (R9). Desenhar o cabo
é leitura de cadastro; afirmar que ele rompeu continua sendo inferência.

### 2.4 Limite: `motivo_desconexao` é pobre

Dos 237 offline, 165 têm o campo vazio e o restante é `NAS-Request`. O motivo do
IXC **não distingue massiva de queda individual**. O sinal que distingue é
simultaneidade + topologia compartilhada. O painel de motivos existe, mas
declara a cobertura ("motivo conhecido em N de M quedas") em vez de fingir que a
distribuição é a realidade.

**Correção de §2.4, medida na #148: existe uma causa melhor, e ela é barata.**
O campo `causa_ultima_queda` vem na **listagem passiva** de
`radpop_radio_cliente_fibra` — nenhuma consulta à OLT é necessária para montar a
distribuição por massiva. E, diferente do `motivo_desconexao` do `radusuarios`
(só `NAS-Request` e vazio), ele **discrimina o tipo de falha**. Amostra de 300
ONUs de produção:

    vazio 213 · "-" 43 · dying-gasp 28 · LOSi/LOBi 9 · reset 3 · ONT 3 · LOS 1

`dying-gasp` é a ONU avisando que perdeu **energia**; `LOS`/`LOSi`/`LOBi` é
perda de **sinal óptico**. Massiva com quase tudo em dying-gasp é falta de luz no
bairro — não se manda viatura. Onde aparece LOS, é fibra. Este é o discriminador
que a triagem precisava. `-` é sentinela de "não informou", igual ao vazio.

A regra de honestidade não muda: cobertura declarada, percentual sobre os
conhecidos, e **o código não conclui a causa** — mostra o que a OLT disse.


### 2.5 Três correções que o dado impôs ao plano original

Medidas na API de produção depois de o plano estar escrito. Cada uma invalidou
uma premissa que parecia óbvia.

**a) `online` tem quatro valores, não dois.** Nos 3.413 logins ativos:
`S`=3.125, `N`=236, `SS`=31, `''`=21. O adapter trata tudo que não é `S` como
offline — o que despeja 52 logins em OFFLINE. Para esta página isso é veneno:
52 quedas fantasma competindo com as ~44 do maior evento real do dia. A
derivação de status foi corrigida em #143 com o porquê registrado no código.

**b) Fração de CTO com denominador pequeno não é evidência.** Distribuição de
logins ativos por CTO: 307 caixas com 1 login, 204 com 2 — **511 das 852 têm no
máximo 2**. Uma CTO de 1 login que cai está "100% fora" e passa trivialmente no
limiar de 70% sem significar nada. Daí o `min_cto_denominator` de §5.

**c) PON é propriedade do login, não da caixa.** Dois caminhos possíveis:
`rad_caixa_ftth.id_interface` está preenchido em só 420 das 1.445 CTOs (29%);
`radpop_radio_cliente_fibra.id_radpop_radio_porta` está em 4.553 de 4.559 (99,9%).
Pior: ao derivar um mapa CTO→PON, **239 das 927 CTOs deriváveis (26%) apontam
para mais de uma PON** — uma caixa pode ser alimentada por mais de uma porta.
Qualquer `Mapping[cto_id, pon_id]` estaria errado em um quarto dos casos. Por
isso a PON entra por login (`Connection.pon_external_id`, alimentada pelo
cruzamento com `radpop_radio_cliente_fibra`) e o degrau PON do detector conta
logins, não caixas. `id_transmissor`, ao contrário, está em 100% das CTOs — o
degrau de OLT pode confiar nele.

### 2.6 Validação dos limiares contra o dado real

Simulação do detector sobre o estado do IXC em 2026-09-08 (3.413 logins ativos,
852 CTOs, 260 quedas com timestamp). Janela de 10 min + mínimo de 5 clientes
produziu **exatamente 2 clusters** — não é ruído:

- `19:46` — 44 quedas, 27 CTOs, **todas no transmissor 1**, caixas cheias
  espalhadas por **1.880 m**;
- `20:05` — 6 quedas, CTO `B41-SP03` inteira fora (5/5).

O primeiro é a prova de que **a escalada por topologia importa mais que o raio
geográfico**: 1.880 m de espalhamento, que nenhum raio de 300 m agruparia, e o
que une o evento é o transmissor comum. O degrau OLT/POP não pode depender de
proximidade.

Depois de pronto, o detector real foi rodado contra esse mesmo estado, com a
topologia real e a PON derivada por login. Saída:

```
[OLT] OLT 1      conf=MEDIA  19:46:05  43 clientes  fração=0,02
      trecho B47 - SP11 → B47 - SP12, B43-SP02, B43-SP03 (+20 caixas)
[CTO] B41-SP03   conf=ALTA   20:13:51   5 clientes  fração=1,00
```

Os dois eventos, com o escopo certo em cada um e **nenhum falso positivo sobre
237 quedas**. A fração de 0,02 em escopo OLT é o comportamento correto e não um
defeito: 43 de ~2.000 logins não é "a OLT caiu" — é um trecho alimentado por
ela, e é por isso que a confiança sai MEDIA. **A UI não pode rotular isso como
"OLT 1 caiu"**; o que orienta o técnico é o trecho suspeito, não o nome do
elemento em escopo.

### 2.8 Sinal óptico: linha de base diária, medição sob evento

O time usa o sinal antes/depois para pegar **fusão mal feita** após um reparo.
O que a API permite:

- **Passivo (diário).** `radpop_radio_cliente_fibra` traz `sinal_rx`, `sinal_tx`,
  `temperatura`, `voltagem`, `data_sinal`; o histórico tem 1,93M linhas de 4.553
  ONUs. Mas **4.551 das 4.554 leituras têm 6–24h** — tudo vem da varredura das
  ~06:30. É linha de base, não leitura pós-reparo.
- **A base é confiável:** uma ONU saudável variou de `-24,43` a `-23,76` em dez
  dias (menos de 0,7 dB). Piora de 3 dB é inequívoca.
- **Ativo (sob demanda).** `POST botao_rel_22991` consulta a OLT ao vivo e
  **atualiza o registro** — dispara e lê pelo caminho normal, sem scraping do
  valor. Traz de bônus `Causa da última queda` (ex.: `dying-gasp`), `Run state`,
  `Last up time`, `Ont distance(m)`, e funciona **com a ONU fora do ar**.
  ~1,7s por chamada em média, 4,1s no pior caso.

**Acionamento sob evento, não sob relógio** (decisão do Paulo): o poll de status
é uma única chamada de listagem e custa quase nada — é ele que carrega o
trabalho e detecta o retorno. **A medição cara só dispara quando um login volta**,
uma vez por cliente. Isso troca "N ONUs por minuto" por "uma chamada por cliente,
uma vez". Sem massiva aberta e sem retorno, nenhuma chamada sai. É a única
exceção autorizada ao §2.7, e é estreita de propósito.

**Armadilhas.** `sinal_rx = 0.00` é **ausência de leitura**, não zero dBm (1.391
dos 4.554 agora) — a base tem que ser a última leitura *válida*, senão todo
cliente caído aparece com "24 dB de perda". ~30% das ONUs não reportam sinal, e
a tela declara essa cobertura. Nem toda ONU devolve a causa: ausência é "a OLT
não informou", não "sem causa".

### 2.9 Escopo: só leitura

Decisão do Paulo (2026-09-08): a ferramenta **não escreve no IXC**. Nada de
`desconectar_clientes`, `su_oss_chamado`, região de manutenção ou aviso ao
cliente. Detecta, agrupa, lista e acompanha o retorno. Única exceção, estreita e
autorizada: o disparo de medição de potência de §2.8, que não grava dado de
negócio — pede à OLT uma leitura.

## 3. Arquitetura

```
apps/network/
├── domain/
│   ├── dto.py            NetworkElementDTO (novo)
│   ├── ports.py          NetworkElementSourcePort (novo), ConnectionSourcePort (+1 método)
│   └── outage.py         detector — Python puro, sem Django  ◄ coração
├── application/
│   ├── drop_tracking.py  diff de snapshot → eventos de queda
│   └── outage_detection.py  orquestra detector + persistência
├── infrastructure/
│   ├── models.py         NetworkElement, ConnectionDropEvent, OutageEvent, OutageAffectedLogin
│   └── repositories.py
└── tasks.py              poll_connection_status (Beat 3min)

apps/integrations/ixc/network_elements.py   IxcNetworkElementSource
apps/integrations/fake/network_elements.py  FakeNetworkElementSource
apps/dashboards/                            view + template + charts da aba
```

Regra que não se dobra: `apps/network/` **não importa** `apps/integrations/`.
O poll resolve o adapter pelo `SourceRegistry`, como todo o resto.

## 4. Modelo de dados

### `NetworkElement` (novo, TenantModel)
`kind` (CTO/POP/PON/OLT/CABLE), `external_id`, `name`, `latitude`, `longitude`,
`parent_external_id`, `parent_kind`, `capacity`, `address`, `project_external_id`,
`status`, `raw_extras`. Unique `(organization, source_type, kind, external_id)`.

### `Connection` (+ colunas promovidas de `raw_extras`)
`cto_external_id`, `cto_port`, `transmitter_external_id`, `concentrator_external_id`,
`latitude`, `longitude`, `disconnect_reason`, `last_disconnection_at`.
Migração de dados faz o backfill a partir de `raw_extras` — não perde histórico.

Mais `pon_external_id`, que **não vem de `radusuarios`**: é o cruzamento com
`radpop_radio_cliente_fibra` (o registro da ONU) pela chave `id_login`, pelo
motivo de §2.5c.

### `ConnectionDropEvent` (novo) — estado de trabalho, não arquivo
Existe pra saber **quem está fora agora e quem já voltou**. Não é série
histórica: nada de índice ou campo que só sirva pra analytics do passado. A
durabilidade mora no `OutageEvent`. Uma linha por queda: `login`, `customer`, `dropped_at`, `restored_at` (null =
ainda fora), `reason`, e o **snapshot da topologia no momento da queda**
(`cto_external_id`, `transmitter_external_id`, `latitude`, `longitude`,
`monthly_amount`). Snapshot porque o cliente pode mudar de CTO depois, e a
massiva de ontem tem que continuar contando o que era ontem.

Existe porque `HistoricalConnection` não registra transições de status — nota já
conhecida do projeto (#20).

### `OutageEvent` + `OutageAffectedLogin` (novos)
A massiva detectada: `started_at`, `ended_at`, `scope` (CTO/PON/OLT/POP/GEO),
`suspected_element` (FK opcional pra `NetworkElement`), `suspected_segment_label`
(texto do trecho suspeito), `confidence`, `affected_count`, `restored_count`,
`mrr_at_risk`. `OutageAffectedLogin` liga a massiva aos eventos de queda.

## 5. Detecção — regra explícita

Entrada: eventos de queda ainda abertos + topologia. Função pura em
`domain/outage.py`, testada isoladamente.

1. **Janela.** Agrupa quedas por bucket de tempo deslizante — default **10 min**,
   configurável. `ultima_conexao_final` dá o instante exato, então a precisão do
   agrupamento não depende do intervalo do poll.
2. **Limiar.** Cluster com **≥ 5 logins** (default, configurável) vira massiva.
   Validado contra o dado real em §2.6: produz 2 massivas, não ruído.
3. **Escopo**, do mais específico ao mais genérico — vence o primeiro que fechar:
   - **CTO** — ≥70% dos logins ativos daquela CTO caíram na janela, **e a caixa
     tem ao menos 3 logins ativos** (`min_cto_denominator`). Abaixo disso a
     fração é aritmética, não evidência (§2.5b): os logins seguem contando em
     `affected_count` e no MRR, só não sustentam "esta caixa caiu";
   - **PON** — ≥2 CTOs da mesma porta PON em escopo CTO. A filiação vem do
     `pon_external_id` **do login** (§2.5c), e uma caixa pode aparecer em duas
     PONs; degrada pro degrau de OLT quando a PON é desconhecida;
   - **OLT** — ≥2 PONs do mesmo transmissor;
   - **POP** — ≥2 OLTs do mesmo POP;
   - **GEO** — nada de topologia fecha, mas os pontos estão a ≤300 m um do outro
     (cluster por haversine). É o caso do rompimento entre caixas.
4. **Trecho suspeito.** Em escopo PON ou GEO com ≥2 CTOs integralmente fora, o
   suspeito é o trecho entre a CTO mais próxima do POP e as demais. Rotula
   "trecho CTO A → CTO B" e lista os cabos do projeto como candidatos, com o
   aviso de §2.3.
5. **Confiança.** Alta quando o escopo fechou por topologia e a fração afetada é
   ≥90%; média quando fechou parcial; baixa quando só o geográfico fechou.
   Fração alta sobre denominador pequeno **não** dá ALTA (§2.5b).
7. **Partida a frio.** Existem hoje 237 logins ativos offline no IXC, muitos
   caídos há **semanas** (`ultima_conexao_final` de agosto, de junho). Sem
   tratamento, a primeira rodada do poll lê tudo isso como queda recente e a
   ferramenta estreia inventando massivas históricas. Por isso: **a primeira
   observação de um login estabelece a linha de base e não gera evento**. Só
   vira queda a transição observada online → offline, ou uma queda posterior ao
   início da observação.

6. **Descartes explícitos.** Login com contrato cancelado/bloqueado não conta
   como queda (é corte, não falha). Queda isolada que não entra em cluster fica
   registrada mas não vira massiva.

## 6. Retorno ao ar

O poll fecha `restored_at` quando o login volta a `online=S`. A massiva mostra
`restored/affected` e **encerra sozinha quando ≥90% voltou**, gravando
`ended_at`. Massivas encerradas ficam no histórico da aba — é o post-mortem.

## 7. Cadência

Task `poll_connection_status`, Beat a cada **3 min**, puxando só
`online=N & ativo=S` (~240 linhas hoje — chamada barata) mais o delta de
`ultima_atualizacao`. O sync completo de 6h continua como está.

## 8. Página

Aba **Quedas & Massivas**, seção Operações, chave de acesso `massivas`.

- KPIs de agora: clientes fora, massivas abertas, MRR afetado, maior massiva.
- **Mapa** (Plotly `scattermap`, tiles OpenStreetMap — exige liberar
  `tile.openstreetmap.org` no CSP; sem dependência nova): clientes fora em
  vermelho, CTOs afetadas, POPs.
- Linha do tempo de quedas por bucket de 10 min.
- Lista de massivas abertas: escopo, elemento/trecho suspeito, confiança,
  progresso de retorno, MRR.
- **O escopo nunca aparece sozinho.** Onde a UI mostra `element_label`, mostra
  junto a fração ("OLT 1 — 2% dos logins da OLT"), e quando
  `suspected_segment_label` não é vazio é **ele** que ganha o destaque visual,
  não o elemento: é ele que diz pra onde o técnico vai. Escopo alto com fração
  baixa significa "algo abaixo desta OLT", nunca "esta OLT caiu" (§2.6). O
  domínio expõe os fatos separados de propósito; costurar a frase é trabalho do
  template, que sabe quanto espaço tem.
- Detalhe da massiva: mapa do recorte + tabela de clientes (nome, contrato,
  plano, telefone, CTO/porta, caiu às, voltou às).
- Painel de motivos com a cobertura declarada (§2.4).
- Auto-refresh por HTMX.
- Entra em `pages.py` (nav + RBAC #65) e em `data_lineage.py` (#67).

## 9. Tarefas

| # | Título | Depende de |
|---|---|---|
| #142 | Topologia de rede: `NetworkElement` + adapter IXC (CTO/POP/PON/cabo) | — |
| #143 | Promover topologia do login e registrar eventos de queda | — |
| #144 | Poll de status de conexão a cada 3 min | #143 |
| #145 | Detector de massivas — agrupamento, escopo e trecho suspeito | #142, #143 |
| #146 | Aba Quedas & Massivas — mapa, massivas abertas e detalhe | #145 |
| #147 | Checagem de retorno e histórico de massivas encerradas | #145 |
| #148 | Sinal óptico e causa de queda por cliente (§2.8) | #143, #145 |

## 10. Correções apuradas na implementação (#142/#143, 2026-09-08)

Medidas contra a API de produção durante a implementação. Onde contradizem as
seções acima, **vale o que está aqui**.

### 10.1 `radusuarios.online` tem quatro valores, não dois

Nos 8.265 logins: `S` 3.129, `SS` 4.870, `N` 245, `""` 21. Nos 3.413 **ativos**:
`S` 3.125, `N` 236, `SS` 31, `""` 21.

`SS` não é sessão simultânea: dos 31 ativos, nenhum tem IP, 28 nunca conectaram
e os 3 restantes pararam em 2025. É o estado de login sem sessão registrada — o
mesmo que 4.839 dos 4.848 logins inativos carregam. Os 21 com campo vazio têm
última conexão em 2021.

Derivação correta: `ativo=N` → BLOCKED; `S` → ONLINE; `N` → OFFLINE;
`SS`/`""` → **UNKNOWN**. Tratá-los como OFFLINE poria 52 clientes fantasma
competindo com as ~44 quedas do maior evento real do dia.

### 10.2 A porta PON vem do login, não da caixa

`rad_caixa_ftth.id_interface` está preenchido em 420 das 1.445 CTOs (29%).
`radpop_radio_cliente_fibra.id_radpop_radio_porta` (o registro da ONU) está em
4.553 de 4.559 (99,9%). E derivando CTO → PON pelos clientes, 239 das 927 caixas
deriváveis (26%) apontam pra **mais de uma** porta: PON é propriedade do login.

Join verificado: `id_login` → `radusuarios.id` casa em 4.081 de 4.081 linhas
úteis, cobrindo 3.323 dos 3.413 logins ativos (97,4%) e 3.096 dos 3.125 online
(99,1%). Nenhum login aponta pra 2 PONs. Daí a coluna
`Connection.pon_external_id`, preenchida pelo adapter de conexões.

`id_transmissor` da CTO, ao contrário, é sólido: preenchido nas 1.445 caixas,
distribuído entre 3 OLTs (1021/272/152).

### 10.3 A planta tem cinco endpoints, não quatro

`radpop_radio` (3 linhas) é a OLT e fecha a escada POP → OLT → PON → CTO:
`radpop_radio.id_pop` → `radpop.id`, `radpop_radio_porta_fibra.id_pop_radio` →
`radpop_radio.id`, e o `id_transmissor` de caixas e ONUs vive no mesmo espaço de
ids (1, 2, 3). Sem ela, os escopos OLT e POP do §5 não fechariam.

O registro da OLT carrega senhas de gerência do equipamento; o schema é
`extra="ignore"` e o `raw_extras` é montado à mão pra que nenhuma credencial
entre no banco do dashboard.

### 10.4 `radpop_radio_porta_fibra` não aceita filtro

Qualquer `qtype` nesse endpoint devolve a página HTML de erro do IXC. Só
paginação pura (`page`/`rp`) funciona.

### 10.5 Sem backfill; a linha de base é a partida a frio

As colunas promovidas (`cto_external_id`, `latitude`, `last_disconnection_at`…)
nascem vazias e o primeiro sync as preenche. Não há migração de dados: a
ferramenta é de tempo real e não responde pergunta sobre o passado.

Em compensação, **a primeira observação de um login é linha de base, não
evento**. Existem hoje 237 logins ativos offline, muitos caídos há semanas
(`ultima_conexao_final` de agosto, de junho). Só vira `ConnectionDropEvent` a
transição observada online → offline, ou a queda cujo `ultima_conexao_final` é
posterior ao início da observação (`baseline_at`). Sem isso a ferramenta
estrearia inventando massivas históricas.

`ConnectionDropEvent` é **estado de trabalho**, não arquivo: quem está fora
agora e quem voltou. A durabilidade da massiva mora no `OutageEvent` (#145).

### 10.6 O poll não puxa o delta de `ultima_atualizacao` (#144)

§7 previa "a lista de offline **mais** o delta de `ultima_atualizacao`". A
segunda chamada foi cortada na implementação: ela não acrescenta sinal. O
retorno ao ar é detectado por **ausência** — quem voltou some da lista de
`online=N`, e é isso que fecha a queda. O delta traria as mesmas linhas com um
custo a mais a cada 3 minutos.

Pela mesma razão o poll **não** reescreve `Connection.status` para ONLINE quando
um login some da lista: a ausência não prova retorno (pode ser bloqueio no ERP)
e o poll não leu o registro dele. Quem responde "está fora agora" é a queda
aberta; o status corrente continua sendo do sync de 6h.

E o poll não paga o enriquecimento de PON (4.559 linhas de ONU por rodada): o
`apply_status_snapshot` do repositório não deixa campo vazio do DTO apagar o que
o sync completo já gravou.

### 10.7 A partida a frio precisou de estado (`ConnectionPollState`)

`baseline_at` (§5.7) não tinha onde morar. Sem ele, só a transição observada
online → offline abre queda — e como o poll lista apenas quem está fora, um
login que já constava OFFLINE no banco, voltou e caiu de novo entre dois polls
nunca abriria evento. Daí uma linha por organização com `baseline_at` (primeira
rodada), `last_poll_at` e `last_success_at`; os dois últimos são o que permite a
tela de tempo real declarar de quando é a foto.

### 10.8 Identidade da massiva é a sobreposição de clientes (#145)

O detector é sem estado e roda a cada 3 min: a mesma massiva é redetectada com
mais gente a cada rodada, e às vezes com o escopo mais alto. A reconciliação
casa cluster e `OutageEvent` aberto por **queda compartilhada**, não por
`(escopo, elemento)` — o escopo muda dentro da mesma ocorrência (o evento real
das 19:46 começaria como CTO e terminaria como OLT, virando três registros). O
raciocínio inteiro está no topo de `apps/network/application/outage_detection.py`.
