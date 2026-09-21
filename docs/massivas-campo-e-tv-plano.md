# Massivas no campo e na TV — plano

**Pedido do Paulo em 21/09/2026**, em seis pontos. O fio que liga todos: a
massiva precisa sair da tela de quem analisa e chegar em quem está na rua ou
olhando a parede.

Duas perguntas diferentes, e o plano não pode confundi-las:

- **na sala** (TV): *está tudo bem? se não, onde?* — leitura a 4 metros;
- **na rua** (técnico): *para onde eu vou e o que procuro quando chegar?* —
  leitura no celular, às vezes sem rede boa.

---

## T1 — O botão de copiar mensagem precisa ser visto

Hoje ele é um link cinza embaixo do card, do tamanho de uma nota de rodapé. Quem
está atendendo uma massiva não vai caçá-lo.

Vira botão de verdade, ao lado de "Ver clientes afetados", com ícone. Continua
secundário em relação ao evento, mas deixa de ser invisível.

## T2 — Ver a massiva no mapa, sozinha

O mapa da aba mostra **todas** as massivas abertas. Quando há três, ninguém sabe
qual ponto é de qual — e é justamente na hora de agir que se precisa olhar uma
só.

Um botão no card abre a massiva **isolada** no mapa: os clientes dela, as caixas
dela, o cabo candidato dela. O detalhe da massiva já faz isso; o que falta é o
caminho curto, sem sair da lista.

## T3 — Limpar a fila de causa do passado

São 56 massivas esperando causa, quase todas de antes de o campo existir. O
Paulo tem razão: ninguém vai lembrar o que foi um evento de 09/09, e **muitas
nem têm nome** — são clusters geográficos, sem elemento de rede.

Chute vira rótulo errado no treino, que é pior que rótulo nenhum. Então elas
saem da fila — mas **dispensadas**, não respondidas: fica registrado que foram
descartadas de propósito, com a data e o motivo. Uma massiva sem causa e uma
massiva descartada são coisas diferentes, e o dia em que o modelo for treinado é
o dia em que essa diferença importa.

## T4 — TV: slide vazio e avanço manual

O slide 3 fica vazio quando não há o que mostrar, e a rotação não espera
ninguém. Dois consertos:

- **slide sem conteúdo sai da rotação**, como o mapa já faz. Tela vazia numa
  parede ensina a sala a ignorar a TV;
- **clicar/tocar avança**, e a rotação recomeça a contar. Quem está de pé na
  frente da TV quer passar para o próximo, não esperar 20 s.

## T5 — TV: novo layout, uma página por massiva

Hoje a TV mostra a lista de massivas num slide e o mapa geral noutro. O Paulo
propôs o formato que faz sentido para quem olha de longe:

- **em cima**, a faixa de números (massivas abertas, clientes fora, etc.);
- **no meio**, uma página por massiva: resumo à esquerda, **mapa daquela
  massiva** à direita.

Cada massiva vira um slide próprio, gerado dinamicamente. Com a rede saudável,
continua o slide de semáforo — silêncio é informação.

## T6 — O link que o técnico abre na rua

Hoje a mensagem leva um link do Google Maps com a coordenada da caixa. Serve
para chegar na rua, mas não mostra **por onde o cabo corre** nem **onde estão as
outras caixas** — que é o que o técnico precisa quando chega.

Vira um link para uma tela de **foco na massiva**: mapa com os clientes, as
caixas, as emendas e o traçado do cabo candidato.

**Decisão pendente, e é de produto:** o técnico não tem login no dashboard.
Três saídas:

1. **link assinado com validade** (o padrão para isto): a URL carrega um token
   que só serve para aquela massiva e expira em algumas horas. Quem receber
   encaminha, e o link morre sozinho;
2. **login para o técnico**: some com o problema, cria outro (gerir contas de
   quem entra e sai);
3. **manter o Google Maps** e aceitar que o técnico vê só o ponto.

A recomendação é a 1, e é assim que este plano segue — mas o link expõe nome de
cliente e mapa da rede a quem tiver a URL, então a validade curta e o escopo de
uma massiva só são parte da decisão, não detalhe de implementação.
