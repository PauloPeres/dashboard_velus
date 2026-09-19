# Causa confirmada e manutenção programada — plano (R9 + R10)

**Decidido com o Paulo em 19/09/2026.** Esta é a primeira vez que o dashboard
aceita **entrada de dados**. Até aqui ele era leitura: tudo o que mostra vem do
IXC ou do warehouse, e nenhuma tela grava o que uma pessoa digitou (ver a
memória do projeto sobre a natureza do sistema).

A troca é deliberada e tem um objetivo declarado: **criar conhecimento**. O
veredito automático de causa (R7 — energia vs. fibra) hoje adivinha e nunca fica
sabendo se acertou; a reincidência (R8) conta eventos sem saber o que eram. Sem
alguém registrando o que a massiva *era de verdade*, nada disso se calibra —
mesmo buraco que já apareceu em churn e em conversas ruins. E, acumulado o
passado, o rótulo vira alvo de modelo.

---

## 1. O que foi decidido

**Causa exclusiva + tags opcionais.** Uma causa obrigatória, lista curta, uma por
massiva — é ela que vira o rótulo do modelo. Tag livre registra a realidade, mas
como rótulo não serve: se tudo é tag, nada é a resposta e não há o que prever.

| Causa (exclusiva) | Quando |
|---|---|
| Rompimento de fibra | cabo partido, seja qual for o motivo |
| Falta de energia | sem energia no equipamento |
| Equipamento (OLT) | falha do transmissor/porta |
| Manutenção programada | janela planejada |
| Falso positivo | não houve queda real |

| Tags (várias) |
|---|
| vandalismo · troca de poste · terceiro (obra, acidente) · concessionária · energia própria/nobreak · chuva |

Assim "rompimento + vandalismo + troca de poste" fica registrado inteiro, e o
modelo aprende a prever a causa. As tags entram como sinal depois, quando
tiverem volume.

**Quem preenche:** a massiva encerra sozinha quando ≥90% dos afetados volta —
ninguém "fecha" à mão. Então a causa é preenchida **depois**, e a tela cobra:
uma fila *"massivas sem causa"* no topo da aba. Pode preencher **qualquer pessoa
com acesso à aba**.

**Manutenção programada (R10):** janela com escopo, elemento e horário,
cadastrada **antes**. Evento que nasce dentro dela já vem marcado como esperado.
A equipe confirmou que cadastra — sem isso, a janela seria código morto que ainda
faz a equipe achar que está protegida.

---

## 2. Regras que a tela não pode quebrar

- **Causa confirmada é o que uma pessoa disse, não o que o sistema achou.** A
  tela nunca preenche sozinha; no máximo sugere (uma massiva dentro de janela de
  manutenção chega com "manutenção programada" pré-selecionada, para confirmar
  ou trocar).
- **O veredito automático não some quando a causa chega.** Os dois ficam lado a
  lado: é a divergência entre eles que calibra o R7. Apagar o palpite ao saber a
  resposta destruiria a única medida de acerto que teremos.
- **Massiva esperada continua aparecendo.** Ela sai do alarme, não da tela: é
  registro do que aconteceu com os clientes, e sumir com ela faria a estatística
  de disponibilidade mentir para melhor.
- **Quem preencheu fica registrado.** Não para cobrar pessoa, e sim porque um
  rótulo sem autor não se audita: quando o modelo aprender errado, é isso que
  permite descobrir de onde veio o erro.

---

## 3. Tarefas

### C1 — Modelo — FEITA (19/09/2026)
`OutageEvent` ganha `confirmed_cause`, `cause_tags`, `cause_note`,
`cause_confirmed_by` e `cause_confirmed_at` — no próprio evento, porque é 1:1
com ele e porque "quais massivas ainda não têm causa" precisa ser um filtro, não
um join.

**A janela de manutenção NÃO virou model novo.** A primeira versão criava um
`MaintenanceWindow` no contexto de rede; ao levar a decisão ao operador, ficou
claro o risco: já existe o cadastro de **evento de rede** (aba de Tendências,
com tipo "Manutenção programada"), e dois cadastros paralelos garantiriam o dia
em que alguém avisa num lugar e a massiva alarma do outro. O `EventoRede` ganhou
`scope` e `element_external_id`, e é ele a janela.

### C2 — Detector — FEITA (19/09/2026)
Ao **criar** a massiva, procura janela que a cubra. Casou: nasce com
`maintenance_event_id` e o rótulo da janela.

Só no nascimento, e de propósito: janela cadastrada depois não marca
retroativamente. O registro guarda o que se sabia quando a massiva apareceu, e
reescrever isso apagaria a diferença entre "avisamos antes" e "explicamos
depois" — que é o que a janela existe para medir.

O `network` não importa o model do `atendimento` (AGENT.md §1.1): pergunta pelo
serviço `apps.atendimento.application.manutencao.janela_que_cobre`.

### C3 — Fila e formulário — FEITA (19/09/2026)
Bloco "massivas esperando causa" no topo da aba, com causa (select), tags
(checkboxes) e observação, gravando por POST comum com redirect.

**Não virou HTMX**, apesar de o resto da aba usar: o projeto não tem nenhum
`hx-post`, e a primeira escrita de dados da ferramenta não é o lugar de estrear
um padrão novo de request. POST + redirect é o que as outras telas de escrita já
fazem.

Fica **fora** do partial de auto-refresh: a cada 3 min o bloco de tempo real se
recarrega, e um formulário meio preenchido não pode sumir debaixo do dedo de
quem está digitando.

### C4 — Manutenção programada na tela — FEITA (19/09/2026)
O formulário de evento de rede (aba de Tendências) ganhou escopo e elemento.
Deixar em branco continua valendo: o evento segue sendo anotação de gráfico, que
é o que todos os anteriores a hoje são. Na aba de Massivas, a massiva esperada
aparece marcada — **sai do alarme, não da tela**.

### C5 — O placar do veredito — FEITA (19/09/2026)
"O palpite automático está acertando?" — acertos sobre comparações, onde as duas
coisas existem.

Duas recusas: só entram massivas em que o veredito **nomeou** energia ou fibra
(ele se cala quando a base é fraca) e cuja causa confirmada é comparável;
manutenção, equipamento e falso positivo ficam fora da conta em vez de contar
como erro, porque não são a pergunta que ele faz. E abaixo de 10 comparações a
tela mostra a fração crua, sem porcentagem — abaixo disso ela oscila mais do que
informa.

### C6 — Testes — FEITAS (19/09/2026)
`tests/test_massivas_causa.py` (19 testes): vocabulário fechado, gravação com
autor, recusa de causa inventada, isolamento entre organizações, permissão pela
aba, o placar e as regras da janela. `tests/test_outage_persistence.py` ganhou a
marcação no nascimento, a janela de outro elemento e a janela cadastrada depois.
