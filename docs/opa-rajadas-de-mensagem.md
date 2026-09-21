# Onde o Opa manda mensagem em cima de mensagem

**Pedido do Paulo em 21/09/2026**, depois da conta do WhatsApp: achar os lugares
em que mandamos uma mensagem logo em seguida da outra — inclusive quando isso
acontece porque **um fluxo chamou outro**, que é onde ninguém enxerga a emenda
olhando um fluxo de cada vez.

Duas medições independentes, que se confirmam:

- **o grafo dos fluxos** (endpoint interno `fluxos-comunicacao/card`, 33 fluxos
  com a estrutura inteira) diz onde a rajada *pode* acontecer;
- **12 a 21/09 de conversa real** (20.000 mensagens da API, 948 conversas) diz
  quantas vezes ela *acontece*.

Definição usada: **rajada** é uma sequência de mensagens nossas, na mesma
conversa, sem nenhuma mensagem do cliente no meio. O tempo não entra — duas
mensagens nossas separadas por dez minutos, sem o cliente falar, continuam sendo
duas notificações no celular dele.

## O tamanho do problema

| medida | valor |
|---|---|
| mensagens enviadas na amostra (10 dias) | 11.808 |
| rajadas | 3.001 |
| conversas com pelo menos uma rajada | **935 de 943** |
| mensagens "a mais" (se cada rajada virasse uma) | **6.771 = 57% do que enviamos** |
| por dia | 677 mensagens · R$ 711/mês a R$ 0,035 |

Cinco conversas com mais de 100 mensagens (1.130 no total, uma delas com uma
rajada de 598) ficaram **fora** da conta: são disparo em massa, não atendimento,
e deixá-las dentro faria a média mentir.

Os 57% são **teto, não meta**: nem toda rajada pode virar uma mensagem só (o
código PIX precisa ficar separado para o cliente copiar, e a transferência para
humano precisa ser dita). O número serve para dimensionar, não para prometer.

Rajadas de **3 mensagens são mais comuns que as de 2** (1.174 contra 1.027) —
não é um escorregão ocasional, é o formato padrão da conversa.

## O que a conversa real mostra (10 dias)

| vezes | sequência |
|---|---|
| 457 | a MESMA cobrança ("consta em nosso sistema que há mensalidades…") **duas vezes seguidas** |
| 388 | menu → "sua sessão expirou!" |
| 191 | "seja bem vindo… seu protocolo é" → menu |
| 183 | "verifiquei que você está com problema de conexão" → "muito obrigado por entrar em contato, iniciaremos o atendimento" |
| 114 | "atendimento temporariamente encerrado" → menu |
| 112 | menu → "ainda está aí? estamos transferindo" |
| 96 | código PIX → "posso te ajudar com mais alguma coisa?" |
| 81 | "atendimento encerrado" → menu → "sua sessão expirou" (trinca) |
| 62 | "posso te ajudar com mais alguma coisa?" → "ainda está aí?" |
| 50 | "fulana alterou o departamento do atendimento" → menu |

## O que o grafo mostra, e a conversa não mostraria

**Rajada atravessando fluxo** (um `eflow` chamando outro): 6 casos. A maior tem
três mensagens e três fluxos —

1. `00 - Verifica se é cliente`: "Olá …, seu protocolo é …"
2. `01 - Diagnóstico de Contrato`: "Verifiquei aqui, que parece que você está com problema de conexão…"
3. `Direto para Suporte`: "Muito obrigado por entrar em contato, iniciaremos o atendimento…"

**Erro de opção manda duas**: em **11 fluxos**, errar a opção dispara o aviso
("Opção inválida!") *e* repete a pergunta inteira. Duas mensagens por engano do
cliente.

**Limite de erro manda duas e troca de fluxo**: 9 combinações em que o nó avisa
("Desculpe não entendi, vou te passar para um atendente") e pula para outro
fluxo, que cumprimenta de novo.

**Um nó só mandando duas**: 3 fluxos têm um `msg` com duas mensagens dentro
(`Direto para o Comercial`, `Facebook Entrada`, `Facebook Retorno`) — aqui a
junção é edição de texto, sem mexer em desenho de fluxo.

## Conferindo a lista do Felipe

| item dele | a medição diz |
|---|---|
| protocolo + menu inicial | **confirmado** — 191x e 183x em 10 dias, nos dois caminhos (cliente e não-cliente) |
| transferência humana: manter | de acordo; mas o par "diagnóstico → transferência" (183x) pode virar um texto só sem perder o aviso |
| Nota Fiscal | não aparece no topo da amostra — volume baixo; o ganho é de experiência, não de custo |
| "posso te ajudar com mais alguma coisa?" | **confirmado** — 96x logo após o PIX, e ainda puxa "ainda está aí?" 62x |
| segunda via / PIX | **confirmado** — trinca "segue código … → PIX → posso te ajudar" 43x + 26x |

## O que faltou na lista, e é maior

1. **A cobrança duplicada** (457x em 10 dias). É a mesma mensagem duas vezes
   seguidas — vale checar se é uma por fatura em aberto; se for, uma mensagem
   com as duas faturas resolve.
2. **O trio do timeout** (~500x somando os pares): "sessão expirou", "ainda está
   aí? estamos transferindo" e "atendimento temporariamente encerrado" dizem a
   mesma coisa em momentos diferentes, e às vezes vêm em sequência com o menu no
   meio.
3. **Aviso de troca de departamento + menu** (50x): quando um atendente humano
   move o atendimento, o cliente recebe o aviso e o menu logo atrás.
4. **Opção inválida repetindo o menu inteiro** (11 fluxos): mandar o aviso com
   as opções curtas, em vez do menu completo, corta uma mensagem por erro.

## Ressalvas

- **A base de cobrança ainda não está confirmada.** Se a Meta cobrar só
  template, o custo é ~30x menor e nada disso compensa em dinheiro — continua
  valendo por experiência. A fatura decide, não a API.
- A amostra é a ponta da coleção (12 a 21/09) e inclui dois fins de semana.
- Nossa base tem 722 mil mensagens ingeridas **sem o texto** (o backfill descarta
  o corpo: "PII sem uso"). Por isso esta análise foi feita contra a API. Virar
  página fixa no dashboard exige guardar o texto — ou um hash dele — das
  enviadas.

---

# Como reproduzir esta análise

Tudo abaixo é leitura (`GET`/listagem). Nada foi alterado no Opa.

## 1. Baixar os fluxos com a estrutura inteira

Este endpoint **não é o da API pública** — é o do painel do atendente, e por
isso autentica por **cookie de sessão do navegador**, não pelo Bearer token que
o dashboard usa. É ele que traz o campo `estrutura`, com todos os nós de cada
fluxo e os textos que cada um envia. A API pública (`/api/v1/...`) não expõe
fluxo nenhum: não existe `chatbot`, `fluxo`, `campanha` nem `automacao`, e a
mensagem não carrega id de fluxo. Foi por isso que o caminho teve de ser este.

```bash
curl --url 'https://opasuite.sorocabana.net.br/atendente/services/fluxos-comunicacao/card' \
  -H 'Accept: application/json, text/javascript, */*; q=0.01' \
  -H 'Content-Type: application/x-www-form-urlencoded; charset=UTF-8' \
  -H 'X-Requested-With: XMLHttpRequest' \
  -b 'opa_language=pt; connect.sid=<COLE AQUI A SUA SESSÃO>' \
  --data-raw 'page=1&rp=100&sortname=nome&sortorder=asc&query=&qtype=nome&oper=L' \
  -o fluxos.json
```

**O cookie não está escrito aqui de propósito.** `connect.sid` é a sessão de uma
pessoa: quem tem a string entra no Opa como ela, e guardá-la num arquivo de
repositório é publicá-la para todo mundo que clonar o projeto. Para pegar a sua:
abra o painel do Opa no navegador, F12 → aba Network → clique em qualquer
requisição → "Copy as cURL". Ela expira; se o comando voltar HTML de login, é
isso.

Resposta: `{"page":1,"total":33,"rows":[...]}`, cada linha com `_id`, `nome` e
`estrutura` (uma **string** JSON com a lista de nós).

Um fluxo específico, se precisar ver isolado:

```bash
curl --url 'https://opasuite.sorocabana.net.br/atendente/services/fluxos-comunicacao/show/<ID_DO_FLUXO>' \
  -H 'X-Requested-With: XMLHttpRequest' \
  -b 'opa_language=pt; connect.sid=<COLE AQUI A SUA SESSÃO>'
```

## 2. O que há dentro de `estrutura`

Tipos de nó encontrados nos 33 fluxos, por frequência: `cond` (42), `eflow`
(42), `init` (33), `msg` (30), `transferirAtendimento` (19), `tag` (14), `perg`
(12), `integracaoErp` (9), `fintechSolution` (3), `pesquisa-satisfacao` (2),
`delay` (2), `httpRequest` (2), `opt_in_opt_out` (1).

O que envia mensagem: `msg` (campo `mensagens[].value` — **um nó pode ter
várias**), `perg` (`pergunta`, e `msg_erro` quando o cliente erra),
`opt_in_opt_out` (`mensagemOptIn`), `pesquisa-satisfacao` (`pergunta`), e os nós
de integração pelo `msg_qtd_erro_excedida`.

O que espera o cliente responder — e portanto **fecha** uma rajada: `perg`,
`opt_in_opt_out`, `pesquisa-satisfacao`.

**Como os nós se ligam, que é a parte que engana:** `next` (e `next1`, no
`cond`) apontam para a **posição do nó no array**, não para o campo `indice` nem
para o `cardIndex` — que existem só em parte dos nós e às vezes discordam da
posição. Lendo por `indice` o grafo parece não ter ligação nenhuma e a busca
devolve zero rajadas; foi exatamente o que aconteceu na primeira tentativa.
`eflow` não tem `next`: ele salta para o `init` de outro fluxo, identificado em
`eflow_id`.

## 3. Rodar as duas análises

```bash
# Estrutural — precisa do fluxos.json do passo 1, roda em qualquer lugar.
python docs/spikes/opa_rajadas_fluxos.py

# Empírico — usa a API pública pelo cliente do projeto, dentro do pod.
kubectl cp docs/spikes/opa_rajadas_reais.py dashboard-velus/<pod>:/tmp/ -c web
kubectl exec -n dashboard-velus deploy/web -- \
  python manage.py shell -v0 -c "exec(open('/tmp/opa_rajadas_reais.py').read())"
```

O empírico pagina `atendimento/mensagem` de trás para frente. Dois detalhes
medidos: `options.limit` aceita **1000** (o docstring do nosso cliente diz 100),
e a listagem global **ignora qualquer filtro** a não ser `id_rota` — data, canal
e ordenação não surtem efeito. Por isso o fim da coleção é achado por bissecção
(~22 chamadas) e a amostra é a ponta dela.

O campo que separa enviada de recebida é `tipoDestinatario`:
`clientes_users` = entregue ao cliente (**enviada por nós**, é o que custa) e
`usuarios` = entregue a um atendente (recebida). Conferido em 6.703 mensagens,
sem terceiro valor.

---

# Anexo A — saída completa da análise estrutural

```
RAJADAS ENCONTRADAS 12 | DELAS ENTRE FLUXOS DIFERENTES 6

==============================================================================
RAJADAS DENTRO DO MESMO FLUXO
==============================================================================

[Direto para o Comercial] 2 mensagens seguidas:
   1. (msg) Seu atendimento foi transferido para o departamento de Comercial, noss
   2. (msg) Caso queira contratar nossos planos entre em https://assine.velusinter

[Fluxo Indique um Amigo] 2 mensagens seguidas:
   1. (msg) *Obrigado por Participar do Nosso Programa de Indicação!* Para facilit
   2. (msg) Seu atendimento foi transferido para o departamento Comercial. Nosso h

[Encerramento] 2 mensagens seguidas:
   1. (msg) Obrigado por escolher a {{nome_empresa}}, estamos sempre trabalhando p
   2. (pesquisa-satisfacao) Avalie nosso atendimento, de 0 a 5?

[Facebook Entrada] 2 mensagens seguidas:
   1. (msg) Olá, tudo bem? Para agilizar seu atendimento, por gentileza nos fornec
   2. (msg) Caso queira, também pode entrar em contato conosco via WhatsApp https:

[Facebook Retorno] 2 mensagens seguidas:
   1. (msg) Seu protocolo é: OPA24352355. Obrigado por contar conosco, espero que 
   2. (msg) ATENDIMENTO ENCERRADO.

[Início] 2 mensagens seguidas:
   1. (msg) Olá eu sou o {{nome_atendente}}, atendente virtual da {{nome_empresa}}
   2. (perg) Por favor, digite o numero da opção selecionada.

==============================================================================
RAJADAS QUE ATRAVESSAM FLUXO (um fluxo chama outro)
==============================================================================

3 mensagens seguidas, atravessando 00 - Verifica se é cliente -> 01 - Diagnóstico de Contrato -> Direto para Suporte:
   1. [00 - Verifica se é cliente] (msg) Olá {{nome_cliente_fornecedor}}, seu protocolo para esse atendimento é
   2. [01 - Diagnóstico de Contrato] (msg) Verifiquei aqui, que parece que você esta com algum problema de conexã
   3. [Direto para Suporte] (msg) Muito obrigado por entrar em contato 😊, iniciaremos o atendimento o ma

2 mensagens seguidas, atravessando 00 - Verifica se é cliente -> 02 - Principal Entrada:
   1. [00 - Verifica se é cliente] (msg) Seja bem vindo a Velus, seu protocolo para esse atendimento é: {{proto
   2. [02 - Principal Entrada] (perg) Por favor, escolha uma das opções abaixo.

2 mensagens seguidas, atravessando 01 - Diagnóstico de Contrato -> Direto para Suporte:
   1. [01 - Diagnóstico de Contrato] (msg) Verifiquei aqui, que parece que você esta com algum problema de conexã
   2. [Direto para Suporte] (msg) Muito obrigado por entrar em contato 😊, iniciaremos o atendimento o ma

2 mensagens seguidas, atravessando Entrada -> 02 - Principal Entrada:
   1. [Entrada] (msg) Olá eu sou o Víctor seu atendente virtual, seja bem vindo a central de
   2. [02 - Principal Entrada] (perg) Por favor, escolha uma das opções abaixo.

2 mensagens seguidas, atravessando Facebook Menu -> Facebook Retorno:
   1. [Facebook Menu] (msg) Entendi. Um de nossos atendentes humanos irá prosseguir com sua solici
   2. [Facebook Retorno] (perg) O que deseja fazer agora?

2 mensagens seguidas, atravessando Manuteção BOT SUPORTE -> 02 - Principal Entrada:
   1. [Manuteção BOT SUPORTE] (msg) Gostaríamos de informar que estamos realizando uma manutenção programa
   2. [02 - Principal Entrada] (perg) Por favor, escolha uma das opções abaixo.

==============================================================================
OUTRAS RAJADAS QUE O GRAFO NÃO MOSTRA COMO SEQUÊNCIA DE NÓS
==============================================================================

-- UM NÓ QUE JÁ MANDA VÁRIAS MENSAGENS (o disparo é do próprio nó) --

[Direto para o Comercial] 2 mensagens num nó só:
   1. Seu atendimento foi transferido para o departamento de Comercial, noss
   2. Caso queira contratar nossos planos entre em https://assine.velusinter

[Facebook Entrada] 2 mensagens num nó só:
   1. Olá, tudo bem? Para agilizar seu atendimento, por gentileza nos fornec
   2. Caso queira, também pode entrar em contato conosco via WhatsApp https:

[Facebook Retorno] 2 mensagens num nó só:
   1. Seu protocolo é: OPA24352355. Obrigado por contar conosco, espero que 
   2. ATENDIMENTO ENCERRADO.

-- ERRO DE OPÇÃO: manda o aviso E repete a pergunta (2 por engano do cliente) --

[02 - Principal Entrada]
   1. (erro) Opção inválida! Escolha uma das opções.
   2. (repete) Por favor, escolha uma das opções abaixo.

[Facebook Menu]
   1. (erro) Ops, não entendi o que você digitou. Digite apenas os números 1, 2 ou 
   2. (repete) Em que podemos te ajudar? Digite o número que corresponde a sua escolh

[Facebook Retorno]
   1. (erro) Ops, digite apenas 1 ou 2, conforme sua necessidade.
   2. (repete) O que deseja fazer agora?

[Falar com atendentes]
   1. (erro) Opção inválida
   2. (repete) Selecione um departamento

[Financeiro]
   1. (erro) Opção inválida! Escolha uma das opções.
   2. (repete) Digite uma das das opções

[Intermediário]
   1. (erro) Opção inválida! Escolha uma das opções.
   2. (repete) Posso te ajudar com mais alguma coisa?

[Início]
   1. (erro) Ops! algo saiu errado.
   2. (repete) Por favor, digite o numero da opção selecionada.

[Massiva]
   1. (erro) Desculpe não entendi, escolha uma das opções.
   2. (repete) 💬 Percebi que você está com um problema generalizado na sua região. Te

[Nota Fiscal]
   1. (erro) Informe uma opção valida!
   2. (repete) Você deseja receber suas notas fiscais?

[Pergunta, Desbloqueio em Confiança]
   1. (erro) Desculpe não entendi.
   2. (repete) Seu contrato de Internet esta reduzido, você gostaria de realizar a li

[Pergunta, Liberação de Redução]
   1. (erro) Desculpe não entendi sua resposta.
   2. (repete) Seu contrato de Internet esta com velocidade reduzida, você gostaria d

-- LIMITE DE ERRO: manda a mensagem E pula para outro fluxo, que fala de novo --

[01 - Diagnóstico de Contrato] (integracaoErp) -> [02 - Principal Entrada]
   1. Desculpe não entendi, vou enviar para nosso menu principal
   2. Por favor, escolha uma das opções abaixo.

[01 - Diagnóstico de Contrato] (integracaoErp) -> [Direto para Triagem - Atendimento Humano]
   1. Desculpe não entendi, vou te passar para um atendente.
   2. Seu atendimento foi transferido para o um atendente humano, nosso horá

[01 - Diagnóstico de Contrato] (integracaoErp) -> [02 - Principal Entrada]
   1. Desculpe não entendi, vou te passar para um atendente.
   2. Por favor, escolha uma das opções abaixo.

[01 - Diagnóstico de Contrato] (fintechSolution) -> [02 - Principal Entrada]
   1. Não consegui encontrar sua fatura, vou te passar para um atendente.
   2. Por favor, escolha uma das opções abaixo.

[01 - Diagnóstico de Contrato] (fintechSolution) -> [Direto para Triagem - Atendimento Humano]
   1. Não consegui encontrar sua fatura, vou te passar para um atendente.
   2. Seu atendimento foi transferido para o um atendente humano, nosso horá

[02 - Principal Entrada] (perg) -> [Direto para Triagem - Atendimento Humano]
   1. Desculpe não consegui entender o que você gostaria, estou te passando 
   2. Seu atendimento foi transferido para o um atendente humano, nosso horá

[Financeiro] (perg) -> [Direto para Financeiro]
   1. Opção invalida, estamos te enviando para um de nossos atendentes.
   2. Seu atendimento foi direcionado ao nosso financeiro. Nosso horário de 

[Financeiro] (integracaoErp) -> [Encerramento]
   1. Desculpe, não consegui encontrar seu cadastro, vou te transferir para 
   2. Obrigado por escolher a {{nome_empresa}}, estamos sempre trabalhando p

[Financeiro] (integracaoErp) -> [Encerramento]
   1. Desculpe, não consegui encontrar seu cadastro, vou te transferir para 
   2. Obrigado por escolher a {{nome_empresa}}, estamos sempre trabalhando p

[Financeiro] (integracaoErp) -> [Encerramento]
   1. Desculpe, não consegui encontrar seu cadastro, vou te transferir para 
   2. Obrigado por escolher a {{nome_empresa}}, estamos sempre trabalhando p

[Início] (perg) -> [Direto para Triagem - Atendimento Humano]
   1. Desculpe não entendi, vou te redirecionar para um atendente.
   2. Seu atendimento foi transferido para o um atendente humano, nosso horá

[Pergunta, Desbloqueio em Confiança] (perg) -> [02 - Principal Entrada]
   1. Desculpe não entendi, vou te passar para o menu principal.
   2. Por favor, escolha uma das opções abaixo.

[Pergunta, Desbloqueio em Confiança] (integracaoErp) -> [Direto para Triagem - Atendimento Humano]
   1. Desculpe não conseguimos realizar a liberação automatica, vou te trans
   2. Seu atendimento foi transferido para o um atendente humano, nosso horá

[Pergunta, Liberação de Redução] (perg) -> [02 - Principal Entrada]
   1. Desculpe não entendi, vou te passar para o menu principal.
   2. Por favor, escolha uma das opções abaixo.

[Pergunta, Liberação de Redução] (integracaoErp) -> [Direto para Triagem - Atendimento Humano]
   1. Desculpe não conseguimos realizar a liberação automatica, vou te trans
   2. Seu atendimento foi transferido para o um atendente humano, nosso horá
```

---

# Anexo B — saída completa da análise sobre conversa real

```
TOTAL NA COLECAO 2889846
CONVERSAS 948 | DIAS 2026-09-12 a 2026-09-21 (10)

CONVERSAS GIGANTES (>100 msgs, fora da conta): 5 com 1130 mensagens

ENVIADAS 11808 | RAJADAS 3001 | CONVERSAS COM RAJADA 935 de 943
MENSAGENS EM EXCESSO (se cada rajada virasse 1) 6771 = 57% do que enviamos
POR DIA: 677 mensagens · R$ 710.96/mês

TAMANHO DAS RAJADAS:
  2 mensagens seguidas: 1027
  3 mensagens seguidas: 1174
  4 mensagens seguidas: 332
  5 mensagens seguidas: 232
  6 mensagens seguidas: 102
  7 mensagens seguidas: 56
  8 mensagens seguidas: 33
  9 mensagens seguidas: 28
  10 mensagens seguidas: 7
  11 mensagens seguidas: 6
  12 mensagens seguidas: 2
  13 mensagens seguidas: 1
  15 mensagens seguidas: 1

PARES MAIS FREQUENTES (A logo depois B, sem o cliente falar no meio):

  386x
      1. {'titulo': 'por favor, clique no botão "ver menu" e esc
      2. sua sessão expirou!

  190x
      1. seja bem vindo a velus, seu protocolo para esse atendim
      2. {'titulo': 'por favor, escolha uma das opções abaixo.',

  183x
      1. verifiquei aqui, que parece que você esta com algum pro
      2. muito obrigado por entrar em contato 😊, iniciaremos o a

  112x
      1. {'titulo': 'por favor, escolha uma das opções abaixo.',
      2. ainda está ai? não se preocupe. estamos transferindo vo

  111x
      1. 📢 aviso importante 📢 atendimento temporariamente encerr
      2. {'titulo': 'por favor, clique no botão "ver menu" e esc

   96x
      1. 00020101021226850014br.gov.bcb.pix2563pixqrcode.sicredi
      2. {'titulo': 'posso te ajudar com mais alguma coisa?', 'o

   62x
      1. {'titulo': 'posso te ajudar com mais alguma coisa?', 'o
      2. ainda está ai? não se preocupe. estamos transferindo vo

   61x
      1. obrigado por escolher a velus, estamos sempre trabalhan
      2. {'titulo': 'avalie nosso atendimento, de 0 a 5?', 'opco

   50x
      1. gislaine alterou o departamento do atendimento para fin
      2. {'titulo': 'por favor, clique no botão "ver menu" e esc

   48x
      1. segue código pix do título com vencimento para 10/09/20
      2. 00020101021226850014br.gov.bcb.pix2563pixqrcode.sicredi

   37x
      1. muito obrigado por entrar em contato 😊, iniciaremos o a
      2. olá! 👋 me chamo moab e vou seguir com o seu atendimento

   33x
      1. poxa, infelizmente ainda não temos cobertura na sua reg
      2. {'titulo': 'por favor, clique no botão "ver menu" e esc

   33x
      1. ainda está ai? não se preocupe. estamos transferindo vo
      2. olá! 👋 me chamo moab e vou seguir com o seu atendimento

   30x
      1. segue código pix do título com vencimento para 21/09/20
      2. 00020101021226850014br.gov.bcb.pix2563pixqrcode.sicredi

   28x
      1. opção inválida! escolha uma das opções.
      2. ainda está ai? não se preocupe. estamos transferindo vo

   26x
      1. ainda está ai? não se preocupe. estamos transferindo vo
      2. gislaine alterou o departamento do atendimento para fin

   25x
      1. {'titulo': 'avalie nosso atendimento, de 0 a 5?', 'opco
      2. ainda está ai? não se preocupe. estamos transferindo vo

   25x
      1. escolha uma opção: 1 - título com vencimento: 10/09/202
      2. ainda está ai? não se preocupe. estamos transferindo vo

   24x
      1. agradecemos pelo contato! 😊 foi um prazer atender você.
      2. {'titulo': 'por favor, clique no botão "ver menu" e esc

   21x
      1. opção inválida!
      2. não consegui encontrar sua fatura, vou te passar para u

   20x
      1. olá! 😊 o rompimento da fibra já foi resolvido e a conex
      2. {'titulo': 'por favor, clique no botão "ver menu" e esc

   19x
      1. desculpe não entendi, vou enviar para nosso menu princi
      2. {'titulo': 'por favor, escolha uma das opções abaixo.',

   18x
      1. temos cobertura no seu endereço sim, abaixo seguem os n
      2. 🔹 350 mbps – r$ 79,90 🔹 750 mbps – r$ 99,90* ⭐ nosso pl

   17x
      1. desculpe não consegui entender o que você gostaria, est
      2. seu atendimento foi transferido para o um atendente hum

   17x
      1. opção inválida!
      2. ainda está ai? não se preocupe. estamos transferindo vo

TRINCAS MAIS FREQUENTES:

   81x
      1. 📢 aviso importante 📢 atendimento temporariamente encerr
      2. {'titulo': 'por favor, clique no botão "ver menu" e esc
      3. sua sessão expirou!

   53x
      1. seja bem vindo a velus, seu protocolo para esse atendim
      2. {'titulo': 'por favor, escolha uma das opções abaixo.',
      3. ainda está ai? não se preocupe. estamos transferindo vo

   43x
      1. segue código pix do título com vencimento para 10/09/20
      2. 00020101021226850014br.gov.bcb.pix2563pixqrcode.sicredi
      3. {'titulo': 'posso te ajudar com mais alguma coisa?', 'o

   39x
      1. gislaine alterou o departamento do atendimento para fin
      2. {'titulo': 'por favor, clique no botão "ver menu" e esc
      3. sua sessão expirou!

   38x
      1. 00020101021226850014br.gov.bcb.pix2563pixqrcode.sicredi
      2. {'titulo': 'posso te ajudar com mais alguma coisa?', 'o
      3. ainda está ai? não se preocupe. estamos transferindo vo

   36x
      1. verifiquei aqui, que parece que você esta com algum pro
      2. muito obrigado por entrar em contato 😊, iniciaremos o a
      3. olá! 👋 me chamo moab e vou seguir com o seu atendimento

   26x
      1. segue código pix do título com vencimento para 21/09/20
      2. 00020101021226850014br.gov.bcb.pix2563pixqrcode.sicredi
      3. {'titulo': 'posso te ajudar com mais alguma coisa?', 'o

   26x
      1. ainda está ai? não se preocupe. estamos transferindo vo
      2. gislaine alterou o departamento do atendimento para fin
      3. {'titulo': 'por favor, clique no botão "ver menu" e esc

   25x
      1. obrigado por escolher a velus, estamos sempre trabalhan
      2. {'titulo': 'avalie nosso atendimento, de 0 a 5?', 'opco
      3. ainda está ai? não se preocupe. estamos transferindo vo

   24x
      1. poxa, infelizmente ainda não temos cobertura na sua reg
      2. {'titulo': 'por favor, clique no botão "ver menu" e esc
      3. sua sessão expirou!
```
