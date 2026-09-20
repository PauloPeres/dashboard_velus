# Calibração do churn — o placar do modelo

**Entregue em 20/09/2026.** Fecha o último buraco da lista de churn: o modelo
apontava clientes em risco e **ninguém sabia se ele acertava**.

É o mesmo buraco que a causa confirmada fechou nas massivas, e a resposta tem a
mesma forma: comparar o que o sistema disse com o que de fato aconteceu.

---

## O que já existia, e por que não bastava

O backtest está no código desde a #125 (`apps/analytics/application/churn_backtest.py`)
e responde bem à pergunta certa: *quem tinha o sinal X numa data passada cancelou
mais do que quem não tinha?*

Só que ele rodava **por linha de comando**. Alguém executava, lia no terminal e o
número morria ali. Isso tem duas consequências:

1. a pergunta "o modelo está acertando?" dependia de alguém lembrar de perguntar;
2. não havia **série** — e um backtest isolado diz "o sinal separa 2,3×",
   enquanto a sequência deles diz se o modelo está melhorando, piorando ou
   parado. É a série que decide investir ou não em recalibração.

## O que entrou

**`ChurnBacktestRun`** guarda cada execução: D0, horizonte, base, cancelados,
taxa da base e os sinais em JSON. Rerodar o mesmo D0 atualiza a linha — o
resultado é determinístico, e duas linhas iguais só confundiriam a série.

Os sinais ficam em JSON, e não em tabela filha, porque a lista muda de forma
quando um sinal entra ou sai do modelo: tabela rígida obrigaria migration a cada
mudança de vocabulário do risco.

**Task semanal** (`run_churn_backtest_for_all_orgs`, segundas às 04:00). **D0 é
`hoje - horizonte`**, e isso não é detalhe: avaliar uma data mais recente mediria
um desfecho que ainda não teve tempo de acontecer, e o resultado sairia
artificialmente bom — todo mundo "ainda não cancelou".

Org sem base não grava linha: base zero não é resultado, é ruído na série.

**O placar na aba de Churn**, com o **lift** em destaque — quantas vezes mais o
grupo marcado cancelou em relação a quem não foi marcado. 1,0 é o acaso; abaixo
de 1,0 o sinal aponta para o lado errado, e a tela diz isso com todas as letras
em vez de esconder num gráfico.

## A separação que a tela não pode perder

Há dois tipos de evidência, e eles **não se misturam**:

| | O que é | Desde quando |
|---|---|---|
| **Score do dia** | o que o algoritmo atribuiu naquela data, lido de `FactChurnRiskDaily` | 11/08/2026 (#123) |
| **Sinal reconstruído** | o que o sinal teria marcado, remontado de chamados e faturas | qualquer data com histórico |

O primeiro mede o algoritmo **como ele rodou**; o segundo, como *supomos* que
teria rodado. Apresentar os dois como a mesma evidência seria repetir o erro que
a #122 corrigiu — projetar o presente para trás. Por isso o score vem em
destaque e os reconstruídos ficam recolhidos, rotulados pelo que são.

## O que este placar ainda NÃO é

Ele mede **separação**, não **calibração**. Dizer "o grupo HIGH cancela 4× mais"
é diferente de dizer "quando o modelo diz 30%, 30% cancelam" — e o score do
churn, hoje, não é probabilidade (está escrito no próprio
`churn_risk.py`). Calibrar de verdade (Platt, isotônica) só faz sentido depois
de algumas semanas de série, e com volume que a base sustente.

Até lá, o número honesto é o lift.
