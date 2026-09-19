# Painel de TV (modo NOC) — plano

**Objetivo:** uma versão tela cheia da aba Quedas & Massivas para ficar numa TV
da sala de operação, rotacionando slides, atualizando sozinha e avisando quando
uma massiva nova abre.

**Escopo deste documento: o painel de NOC.** Ficou decidido que haverá também um
painel executivo, em rota separada — ele é frente posterior e tem documento
próprio quando chegar a vez.

**A pergunta que o painel responde**, a 4 metros e em 2 segundos:
*está tudo bem? se não, onde?* Tudo que não serve a isso é enfeite e sai.

A base de domínio desta seção veio de uma consultoria com especialista em NOC de
ISP (2026-09-18). **É conhecimento de um consultor, não fonte verificada** — os
números (segundos de rotação, px, dBm) são pontos de partida a calibrar na sala
de vocês, não medidas.

---

## 1. Decisões tomadas (2026-09-18)

**Audiência: dois painéis, em rotas separadas.** Um de NOC e um executivo —
nunca uma tela híbrida. **Começamos pelo de NOC**; o executivo é frente
posterior, e o que este documento descreve é o de NOC. Consequência de projeto:
a casca (rotação, barra fixa, frescor, pareamento) tem que nascer reutilizável,
porque vai servir os dois; o conteúdo dos slides é que é específico.

**Plantão 24/7 na sala.** Sempre há alguém vendo, então o alerta pode viver na
TV. O Telegram deixa de ser o canal principal e vira **escalonamento**: só
severidade crítica ou evento que passou N minutos sem ninguém reconhecer. Isso
simplifica P7 e reduz o risco de fadiga por push.

**Acesso: pareamento por QR code, não URL comprida.** Digitar uma URL com token
numa TV é inviável. O fluxo decidido:

1. Na home pública de `velus.seujaime.com` (antes do login) há um botão
   **MODO TV**. O mesmo botão existe dentro da página Quedas & Massivas, para
   quem já está logado.
2. A TV abre a tela de pareamento e mostra um **QR code** apontando para
   `velus.seujaime.com` com um código de pareamento como parâmetro.
3. Alguém escaneia com o celular, **faz login normalmente**, e ao autenticar
   aprova aquela TV.
4. A TV, que fica consultando o status do pareamento, recebe a credencial de
   display e entra no painel.

É o mesmo padrão de login de TV que Netflix e YouTube usam (device authorization
grant). Dois cuidados que precisam estar no código, senão o fluxo vira buraco:

- **O QR carrega um código de pareamento, nunca a credencial de acesso.** Quem
  fotografa a tela não ganha nada: a credencial é emitida para o navegador da TV
  que iniciou o pedido, por um canal separado. Sem isso, qualquer visitante que
  fotografe a TV entra no dashboard.
- **O código de pareamento é efêmero e de uso único** (sugestão: 5 min), e a
  credencial de display resultante é **revogável** — precisa de uma tela que
  liste os dispositivos pareados e permita derrubar um.

Fica pendente de decidir na implementação: se a credencial de display expira
sozinha (e em quanto tempo) ou se vive até ser revogada.

---

## 2. Anatomia do painel

### Barra fixa, visível em todos os slides
- **Clientes offline agora** — número absoluto e % da base. É o número mais
  importante da tela.
- **Massivas abertas** — contador. Zero = verde discreto; ≥1 = vermelho.
- **Idade do dado** — contador *crescente em tempo real* ("atualizado há 47s").
  Timestamp estático o cérebro ignora; contador que anda, não.
- **Saúde da coleta** — verde/amarelo/vermelho.

### Slides que rotacionam
1. **Semáforo** (slide casa) — offline agora, massivas abertas, maior evento,
   estado da coleta. Com a rede saudável, essa tela fica calma e parada: silêncio
   é informação.
2. **Massivas abertas** — ordenadas por impacto: escopo, nº afetados, % do
   elemento, há quanto tempo, veredito de causa (R7 da frente de rota). Sem
   massiva, um "nenhuma massiva aberta" grande e verde — nunca um slide vazio.
3. **Mapa ao vivo** — só entra na rotação **quando há massiva aberta**. Com a
   rede de pé, mapa é decoração e queima um ciclo da rotação.
4. **Rede** — OLTs e POPs, tempo desde o último contato, o que está degradado mas
   não caído.
5. **Atendimento agora** (Opa! Suite) — fila, quem espera há mais tempo e **pico
   anormal de contatos**. Segundo o especialista, o pico de atendimento costuma
   detectar massiva antes do poll de rede; e quando uma massiva abre, saber
   quantos já ligaram dimensiona o custo.
6. **Últimas 24h** — quedas por hora, para dar contexto ao "isso é normal pra
   essa hora?".

Slides de campo (OS do dia, técnicos em rota) ficam para depois: só entram se a
informação for acionável por quem está na sala.

### Tempos
- **Rotação: 20 s** por slide; **40 s** no slide de massivas quando há evento
  aberto. Abaixo de 12 s ninguém lê; acima de 45 s esquece-se que a tela muda.
- **Dados: fetch a cada 60 s**, sem recarregar a página (recarga pisca e faz
  perder o ponto de leitura). O poll real é de 3 min, então 60 s só suaviza a
  latência de exibição.
- **Fixar slide** quando há evento crítico — a rotação não pode pular justo a
  tela que interessa durante a crise.

---

## 3. A regra inegociável: o painel não pode mentir com dado velho

É o pecado capital de painel de NOC, e no nosso caso não é hipotético: o sync do
IXC já falha de forma intermitente (host dual-stack sem rota IPv6, página HTML de
erro do IXC). Quando a coleta morre, a tela precisa gritar, não ficar verde.

- Idade do dado até 1× o intervalo esperado: verde. Até 2×: amarelo. Acima:
  **faixa vermelha "DADO DESATUALIZADO — última coleta há 12 min"** e véu sobre
  os números.
- **Dado ausente mostra "—", nunca 0.** Zero é uma afirmação; ausência não é.
- Distinguir **"a coleta falhou"** de **"a coleta está ok e a rede caiu"** — a
  ação é oposta.
- **Sem conexão com o servidor** precisa aparecer como tal. Tela branca é
  indistinguível de "tudo normal e vazio".

Isso é a continuação direta da regra que a tela já segue hoje (o carimbo "de
quando é a foto"), só que em corpo 40px.

---

## 4. Alertas sem fadiga

Três níveis, e só o de cima interrompe:

| Nível | Gatilho (a calibrar) | O que faz |
|---|---|---|
| INFO | ≥3 clientes, mesma CTO | entra na lista, sem destaque |
| ATENÇÃO | PON inteira ou ≥1% da base | card destacado no topo |
| CRÍTICO | OLT/POP, ou ≥5% da base | takeover de 30-60 s + som **uma vez** |

Regras que fazem o alerta continuar funcionando depois do primeiro mês:

- **Supressão por persistência:** não alarmar antes de ~5-10 min de evento (mata
  flap e reboot de OLT) — mas mostrar na lista desde o primeiro segundo.
- **Um evento, um alerta.** Massiva que cresce atualiza o card, não dispara de
  novo.
- **Som uma vez só.** Som em loop é a causa nº 1 de alguém desligar a caixa —
  e aí perde-se o único canal que alcança quem não está olhando.
- **Piscar por tempo limitado** (10 s e depois sólido), nunca acima de 3 Hz —
  fadiga e risco de fotossensibilidade.
- **Takeover volta para a rotação.** Takeover permanente mata o painel.
- **Modo manutenção** (R10 da outra frente): sem ele, toda janela programada
  treina a equipe a ignorar a tela.
- **Reconhecimento:** alguém dá "ciente" pelo celular e o card vira "em
  tratamento com Fulano". Converte o painel de gritador em coordenador — segundo
  o especialista, é o que mais aumenta o uso real.
- **Escalonamento para Telegram** quando: severidade crítica, fora do horário,
  N minutos sem reconhecimento, ou cliente crítico afetado. *TV = consciência
  contínua; push = exige ação de alguém.*

---

## 5. Legibilidade a 4 metros

- **Altura de caractere ≈ distância ÷ 200.** A 4 m → ~20 mm. Numa TV 50" 1080p
  isso é ~35 px. Então: texto de apoio ≥ 32-36 px, rótulos ≥ 40 px, **número
  herói 120-200 px**. Em 4K, renderizar em 1920 lógico e deixar o browser
  escalar.
- **1 número herói + no máximo 5-7 itens por slide.** Densidade é o erro mais
  comum. Se não cabe, vira outro slide.
- **Tema escuro**, fundo quase-preto (#0F1115, não #000 puro, que causa halo em
  LCD). Contraste ≥ 7:1. Vermelho saturado sobre preto sangra — usar vermelho
  claro/alaranjado.
- **Daltonismo (~8% dos homens):** nunca só verde vs vermelho. Segundo canal
  sempre: ícone, posição, tamanho e o estado escrito. Teste: em escala de cinza o
  painel ainda precisa funcionar.
- **Sem scroll automático de tabela** (ninguém lê linha que se move), sem
  tooltip (não existe mouse), sem transição longa.
- **Burn-in:** deslocar levemente o layout ao longo do dia; nada estático por
  meses no mesmo pixel.

Nota: isso conflita com o visual atual do dashboard (tema claro, Tailwind, texto
pequeno, tooltips). O painel é **uma tela nova**, não a tela atual em fullscreen.

---

## 6. Tarefas

### P0 — Pareamento por QR code — FEITA (19/09/2026)
Botão MODO TV na home pública e na página de massivas; tela de pareamento com QR;
aprovação por quem faz login; emissão da credencial para o navegador da TV;
listagem e revogação de dispositivos pareados. Ver os dois cuidados da seção 1.
**Bloqueia:** P2 (a casca precisa de alguém autenticado para carregar dado).

**Como ficou.** `DisplayDevice` (em `tenancy`, não em `dashboards`: é identidade,
não tela) guarda **três segredos com papéis diferentes**, e a confusão entre eles
era o buraco a evitar:

| Segredo | Onde vive | Para que serve |
|---|---|---|
| `code` | **na tela da TV** | ser lido e digitado/escaneado. Efêmero (5 min), de uso único |
| `device_token` | cookie da TV | provar, na consulta de status, que é a TV que pediu o pareamento |
| `display_token` | cookie da TV | a credencial final, revogável |

Os três são gravados como **hash**: um dump do banco não vira TV alheia no ar. E
o código **vira NULL ao ser usado** — código que sobrevive ao uso dá a alguém uma
segunda chance de aprovar a mesma TV.

Quem fotografa a tela da TV não ganha acesso: ganha, no máximo, a chance de
**aprovar** aquela TV, o que exige login e acesso à aba do painel. A credencial
sai pelo outro canal, para o navegador que tem o `device_token`.

**A credencial não expira sozinha**, e isso é decisão, não esquecimento: a TV
fica ligada meses, e uma expiração silenciosa viraria painel apagado de
madrugada sem ninguém para reparear. Ela morre por revogação explícita — a lista
em Configurações existe desde o primeiro dia, com o último contato de cada TV.

**Dependência nova:** `segno` (QR em Python puro, sem dependências próprias,
SVG inline). Justificativa exigida pelo AGENT.md §0.3: gerar o QR aqui dentro
evita mandar o código de pareamento para um serviço externo de QR — que seria
publicá-lo fora.

**Alfabeto do código sem 0/O/1/I/5/S:** ele é lido de longe, numa TV, e às vezes
ditado por telefone. Ambiguidade aqui vira "não consigo parear" na sala.

### P1 — Endpoint único de snapshot — FEITA (19/09/2026)
Um JSON com tudo que os slides precisam, cache curto. A rotação **não** pode
multiplicar consulta ao banco e ao IXC — a TV não pode virar carga.
**Depende de:** nada.

**Como ficou.** Cache de 20 s por painel **e por organização** — nunca global, ou
uma TV veria o número de outra empresa. Duas TVs na mesma sala, mais o navegador
de quem está conferindo, viram uma consulta só.

O `gerado_em` vai **dentro** do snapshot: servido do cache, ele continua dizendo
a idade real do dado. Se viesse do relógio de quem lê, o cache transformaria dado
de 19 s atrás em "agora".

Responde **403 com corpo JSON**, nunca 302 para o login: um redirecionamento aqui
viraria painel congelado sem explicação na parede.

### P2 — Casca do painel: rota, layout escuro, barra fixa, rotação — FEITA (19/09/2026)
Tela cheia, tema próprio, barra fixa com os 4 indicadores, motor de rotação com
fixar-slide. Sem dado ainda — só a casca e o relógio.
**Depende de:** P0, P1.

**Como ficou, e o que fica valendo para o painel executivo.** A casca é de
`PanelSpec`, não do NOC: rota (`/paineis/<key>/`), pareamento, cache, barra,
rotação e frescor nascem uma vez. Um painel novo registra um spec com seu
snapshot e seus slides e herda tudo — que é exatamente o que **não pode
divergir** entre as duas telas.

Um slide pode se declarar condicional (`only_when`): o mapa só entra na rotação
quando há massiva aberta. Slide sem pergunta treina a sala a ignorar a TV.

**O relógio da rotação é independente do relógio do dado.** A tela troca de slide
mesmo quando o servidor para de responder, e quem avisa que o conteúdo envelheceu
é o contador de frescor. O contrário congelaria a TV num slide com números
antigos e cara de vivo.

E a página **se recarrega inteira a cada 15 min**: TV de NOC fica meses ligada, o
navegador acumula memória e o JS engasga (é o P9, adiantado porque é uma linha).

### P3 — Slides 1, 2 e 6 (semáforo, massivas, últimas 24h) — FEITA (19/09/2026)
O núcleo. Com isso o painel já é útil.
**Depende de:** P2.

**Como ficou.** Semáforo (OK gigante e verde, ou o número de massivas em
vermelho com a maior delas), massivas abertas ordenadas por quem tem mais gente
fora — a ordem em que a sala deve agir, no máximo 5 linhas —, mapa (só com
evento) e as últimas horas.

O tema escuro dos gráficos é aplicado **no cliente**: o mesmo gráfico serve a aba
(fundo claro) e a TV (fundo escuro). Duplicar a figura no servidor faria as duas
divergirem no dia em que uma fosse corrigida.

### P4 — Frescor e falha de coleta (seção 3) — FEITA (19/09/2026)
Contador crescente, faixa de dado velho, véu, "—" em vez de 0, tela de sem
conexão. **Vai junto com P3, não depois** — painel que pode mentir não sobe.
**Depende de:** P2.

**Como ficou.** A idade é um contador que **anda**, recalculado a cada segundo a
partir do que o servidor disse. Passando de 10 min, o número fica âmbar e uma
faixa atravessa a tela: *"Dado velho — a tela não sabe o que está acontecendo
agora."*

Quando a idade é **desconhecida** (nenhum poll bem-sucedido), a barra mostra
**"—"**. Zero ali seria a mentira mais confortável da tela: parece dado fresco.

Se o snapshot para de responder, o contador continua subindo a partir da última
leitura boa — é ele que vai acusar. O pior desenho possível seria deixar o número
parado como se nada tivesse acontecido.

### P5 — Slide de mapa, com pulo automático — FEITA (19/09/2026)
Reaproveita o mapa atual, já escopado às massivas abertas. Sai da rotação quando
não há evento — via `only_when` do `SlideSpec`, que é da casca e serve a qualquer
painel.
**Depende de:** P3.

### P6 — Alertas: níveis, supressão, dedup, takeover — FEITA (19/09/2026)
Sem som ainda — só visual, para medir se o critério de severidade está calibrado
antes de fazer barulho na sala.
**Depende de:** P3.

**Como ficou.** Os três níveis saem de `panels/alerts.py`, com todos os limiares
em constantes nomeadas num lugar só — eles vieram da consultoria e são **ponto
de partida a calibrar na sala**, não medida.

As quatro recusas, que importam mais que os limiares:

- **só CRÍTICO toma a tela.** É o que separa "largue o que está fazendo" de
  "está na lista";
- **evento com menos de 5 min não interrompe** (supressão por persistência:
  mata flap e reboot de OLT) — mas aparece na lista desde o primeiro segundo.
  Suprimir o alarme não é esconder o evento;
- **um evento, um alerta.** A TV guarda o que já anunciou em `localStorage`, e
  por isso a massiva não volta a tomar a tela nem depois da recarga de 15 min. O
  registro é por **tela**, não por servidor: duas TVs na sala devem anunciar
  cada uma a sua vez;
- **manutenção programada nunca passa de INFO** e **reconhecida para de
  gritar** — as duas pelo mesmo motivo: alarmar o que já foi avisado ou já tem
  dono é o caminho mais curto para a equipe ignorar a tela.

O piscar dura 10 s e fica sólido, a ~1 Hz — nunca acima de 3 Hz, por fadiga e
fotossensibilidade. O takeover devolve a tela em 45 s: takeover permanente mata
o painel, porque a sala para de ver o resto.

### P7 — Som e escalonamento para Telegram
Só depois de P6 rodar algumas semanas e os limiares estarem ajustados. Com
plantão 24/7, o Telegram é **escalonamento**, não canal principal: severidade
crítica ou evento sem reconhecimento há N minutos.
**Depende de:** P6.

### P8 — Reconhecimento ("ciente, Fulano está tratando") — FEITA (19/09/2026)
Primeiro ponto de entrada de dados do painel. Cruza com R9 da frente de rota
(causa confirmada) — mesmo modelo, decidir junto.
**Depende de:** P6, R9.

**Como ficou.** Campo próprio (`acknowledged_by`/`acknowledged_at`), e **não**
junto da causa confirmada, apesar de terem chegado no mesmo dia: reconhecimento
é afirmação sobre **agora** ("estou tratando") e causa é sobre o **passado**
("era rompimento"). Guardar as duas no mesmo lugar perderia a única coisa que o
reconhecimento mede — o tempo entre o evento aparecer e alguém assumir.

O **primeiro a assumir é quem fica**: sobrescrever apagaria quem realmente pegou
o evento. E o `next` do formulário só aceita caminho interno, porque o link chega
do celular de quem está na rua e URL absoluta ali viraria redirect aberto.

No painel, o evento reconhecido troca a borda para índigo e mostra "em
tratamento com Fulano" — continua na lista, para de interromper. Segundo o
especialista, é o que mais aumenta o uso real do painel: evita a terceira ligação
para o mesmo técnico.

### P9 — Operação da TV — PARCIAL (19/09/2026)
Kiosk mode, autostart, watchdog que recarrega se o JS congelar, reload de
madrugada. TV de NOC fica meses ligada e o Chrome trava.
**Depende de:** P2.

**Feito:** a recarga periódica da página (15 min), que já cobre o JS engasgado e
traz os slides redesenhados. **Falta** o lado de fora do navegador: kiosk mode,
autostart e watchdog do sistema operacional — isso é configuração da máquina da
sala, não código.

### P10 — Slide de atendimento (Opa! Suite)
Fila, espera mais longa e pico anormal de contatos.
**Depende de:** P3.

---

## 7. O que deliberadamente NÃO entra

- MRR, churn, metas — muda a audiência da tela e a operação para de olhar.
- Pizza, donut, 3D, série com mais de ~6 linhas, tabela com mais de ~8 linhas.
- Dado sensível de cliente (nome completo, CPF, endereço) numa TV que visitante
  enxerga. Login e ID de contrato bastam.
- Qualquer slide que ninguém tenha agido por causa dele em 3 meses. Painel de NOC
  bom **encolhe** com o tempo.
