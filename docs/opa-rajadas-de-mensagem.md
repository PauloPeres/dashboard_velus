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
