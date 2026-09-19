# Achados — pauta Premium / cortesia / reajuste

**Data:** 17/08/2026 · **Fonte:** IXC (API) + warehouse Velus (prod) · **Base:** 3.450 contratos ativos+bloqueados (2.901 ACTIVE + 549 BLOCKED), MRR bruto R$ 343.609

Status: parcial. Os números 1, 3 e 5 estão fechados; o 2 tem o volume medido mas o valor em R$ é estimativa; o 4 só fecha quando setembro terminar de ser emitido.

---

## 1. Penetração do PREMIUM (R$ 9,90) — **0,78% da base**

Produto IXC id **122**, descrição `PREMIUM`, `preco_base` R$ 9,90, ativo. Vinculado a contratos pela tabela `vd_contratos_produtos` (não por `cliente_contrato_servicos` — por isso não aparece no MRR de adicionais do dashboard).

| | |
|---|---|
| Contratos que já assinaram | **46** |
| Ainda ativos/bloqueados | **27** |
| Já cancelados | 19 (41%) |
| Penetração na base ativa | **0,78%** |
| MRR gerado | **R$ 267,30/mês** |

**Quando foi vendido:** jan/25 = 6 · fev/25 = 25 · mar/25 = 13 · abr/25 = 1 · mai/26 = 1. Ou seja: houve um empurrão de ~3 meses no começo de 2025 e depois **parou**. Não é um produto com baixa adesão — é um produto que ninguém está vendendo há 16 meses.

Planos de quem tem: 40 no "750 MB 99,90", 5 no "350 MB 79,90", 1 no 1GIGA.

Para comparação, a penetração de **qualquer** serviço adicional pago (`cliente_contrato_servicos`) é 325 de 3.450 = **9,4%**, R$ 14.795 de MRR — e a maior parte disso é mudança de endereço, renegociação e desconto, não produto recorrente.

> Com 0,78%, a alavanca é vender. A hipótese "a cortesia está competindo com o Premium" só se sustenta se o Premium estiver sendo oferecido — e o histórico de vendas diz que não está.

---

## 2. Isenções — o volume não é "primeira vez", é praticamente tudo

**Não existe campo de isenção no IXC.** Não há flag, não há motivo, não há lançamento. Então não dá pra contar isenção diretamente; dá pra contar **o que foi cobrado** e comparar com **o que aconteceu**.

### Cobertura de cobrança por tipo de evento (12 meses)

| Evento | OS | Cobranças | R$ | Cobertura |
|---|---|---|---|---|
| Manutenção técnica | 1.747 | 12 | 575,00 | **0,7%** |
| Passagem de cabo | 51 | 2 | 200,00 | 3,9% |
| Mudança de cômodo | 33 | 1 | 25,00 | 3,0% |
| Roteador adicional | 37 | 7 | 210,00 | 18,9% |
| Mudança de endereço | 153 | 145 | 5.214,43 | **94,8%** |
| **Total** | **2.021** | **167** | **6.224,43** | **8,3%** |

**O ponto não é que ninguém cobra.** Mudança de endereço é cobrada em 95% dos casos — quando existe regra clara, o processo executa e o dinheiro entra. Manutenção técnica, a 0,7%, não tem regra: tem costume.

Método: razão entre contagem de OS por assunto e contagem de lançamentos casados por descrição na mesma janela — **não é um join por evento**. Serve pra ordem de grandeza e tendência, não pra auditar caso a caso. Os pares "Agendar X"/"X" são o mesmo atendimento em duas etapas; contei só a execução.

Some 1.255 trocas de equipamento no mesmo período (item 3), que não têm nenhuma cobrança associada.

### Como sei que não está cobrado em outro lugar

A dúvida é justa — a cobrança poderia estar escondida em outro caminho. Testei os quatro possíveis, todos em produção:

1. **Cobrança gerada pela OS** — `cliente_contrato_servicos` tem o campo `id_oss_chamado` que amarra o lançamento à ordem de serviço. Dos **1.870 lançamentos existentes, 0 têm `id_oss_chamado` preenchido.** Nenhuma OS na história do IXC gerou lançamento financeiro por esse caminho.
2. **Recebível apontado pela própria OS** — o campo `id_receber` da OS. Das 1.747 manutenções, 1.983 agendamentos, 940 retiradas, 51 passagens de cabo e 33 mudanças de cômodo dos últimos 12 meses: **0 têm `id_receber`**. As únicas 762 OS com recebível no ano são de assunto "Cobrança Manual 01/02" — cobrança de inadimplência, não de serviço.
3. **Valor lançado na própria OS** — o campo `valor_total` da OS. **0 OS com valor > 0 em 12 meses**, em toda a base de 20.938 OS.
4. **Fatura avulsa** — 1.528 faturas sem contrato em 12 meses, R$ 380.039. Não são taxa de serviço: 504 delas (R$ 301 mil) são acima de R$ 300, com 332 repetições do mesmo valor de R$ 550 (padrão de parcela/link dedicado), e as observações que existem falam de "multa de rescisão" e período de uso. Nenhuma menciona visita, troca ou deslocamento.

Isenção, quando aparece, aparece **em texto livre na OS**: 110 casos distintos em 12 meses (~9/mês), estáveis mês a mês. Dos 22 que citam valor, a média é R$ 225 — mas o que está sendo isentado ali é **multa de fidelidade** (R$ 374,95 recorrente), taxa de mudança de endereço/cômodo (R$ 50–90) e fatura perdoada. Extrapolando, ~R$ 25 mil/ano — número frágil, use como ordem de grandeza, não como valor.

> **A regra de "primeira grátis" não é o problema — a ausência de regra em manutenção é.** Onde existe regra (mudança de endereço) cobra-se 95% das vezes; onde não existe (manutenção técnica), 0,7%. E o custo da cortesia não está na taxa não cobrada (~R$ 6 mil/ano): está no item 3, nos R$ 393 mil de equipamento.

Complemento: 63 contratos ativos têm a flag `isentar_contrato = S` no IXC (isenção da mensalidade inteira, coisa diferente).

---

## 3. 🔴 Trocas de equipamento — **0,36 por cliente/ano · R$ 9,50/mês de custo**

Método: "evento de entrega" = data distinta de comodato por contrato; troca = todo evento depois do primeiro.

| Últimos 12 meses | |
|---|---|
| Trocas | **1.255** |
| Contratos que trocaram | **1.045** (30% da base ativa) |
| Valor dos equipamentos entregues | **R$ 393.176** (~R$ 313/troca) |
| **Por cliente ativo/ano** | **0,36 trocas · R$ 114/ano · R$ 9,50/mês** |

Série anual: 2023 = 727 · 2024 = 1.357 · 2025 = 1.324 · 2026 até ago = 696.

Distribuição: 876 clientes trocaram 1×, 144 trocaram 2×, 21 trocaram 3×, cauda até 11×.

Top produtos entregues em troca: ONU HG6143 (452, R$ 110 mil) · HG6145F3 (385, R$ 84 mil) · roteador Fiberhome WiFi-6 (229, R$ 35 mil) · SmartPro 4khd (190, R$ 34 mil).

> **R$ 9,50/mês por cliente é o teste do Premium.** Um produto a R$ 9,90 cobrindo equipamento empata com o custo médio da base — e quem assina não é a base média, é quem já quebrou aparelho. Sem carência ou limite de trocas, o produto nasce no vermelho.

Ressalva: inclui roteador adicional e setup box, não só ONU queimada. Dá pra separar por produto se a decisão depender disso.

---

## 4. Downgrades desde o Dia dos Pais — **não há onda (ainda)**

- **OS "Alteração de Plano": 8 em agosto, 4 desde o dia 09.** Média dos 12 meses anteriores: 8/mês. Sem desvio.
- **Queda de mensalidade faturada** (contrato a contrato, mês a mês): ago/26 = **25 contratos, R$ 1.167**. Baseline dos 12 meses: 22 a 68 quedas/mês, R$ 730 a R$ 1.770.
- Altas superaram quedas em **todos** os 12 meses. Em agosto: 96 altas, +R$ 4.388.
- set/26 = 15 quedas, R$ 870 — **mas só 2.012 das ~3.100 faturas foram emitidas**. Setembro é o mês que decide.

Método: o warehouse não historiza plano (o `simple_history` retém 10 dias por causa do cronjob de limpeza), então usei o **valor faturado por contrato** como proxy — que é o que o cliente efetivamente paga. Refazer em ~10 dias, com setembro emitido.

Se esse número tiver que existir de forma confiável daqui pra frente: é uma issue pequena, historizar `monthly_amount` num fato mensal.

---

## 5. Completam 12 meses nos próximos 90 dias — **143 contratos, R$ 14.691**

| Mês do aniversário | Contratos |
|---|---|
| ago/26 (restante) | 25 |
| set/26 | 47 |
| out/26 | 51 |
| nov/26 (até dia 15) | 20 |

~48 por mês, ~11 por semana — cabe fazer reajuste por aniversário sem estourar o suporte.

Contexto: **32,3% da base (1.114 contratos) tem menos de 12 meses**; tenure mediano 2,1 anos. Nos últimos 12 meses entraram 1.478 instalações e saíram 1.079 solicitações de cancelamento.

---

## Como reproduzir

Todas as consultas rodaram contra produção via `kubectl exec -n dashboard-velus deploy/web -- python manage.py shell`, com `set_current_organization(Organization.objects.get(slug="velus"))`. Endpoints IXC usados: `produtos`, `vd_contratos_produtos`, `vd_contratos`, `cliente_contrato_servicos`. Modelos: `Contract`, `Invoice`, `Ticket`, `ContractEquipment`, `FactContractStatusDaily`.
