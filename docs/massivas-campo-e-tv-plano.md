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

**Decidido pelo Paulo: link assinado, 24 horas.** As outras saídas eram criar
login para cada técnico (troca um problema por outro: gerir contas de quem entra
e sai) ou manter só o ponto do Google Maps.

**Como ficou.** O token é assinado com a `SECRET_KEY`, carrega a massiva **e** a
organização, e expira em 24 h — o que cobre o turno e a virada de plantão, e
garante que um link esquecido num grupo não sirva na semana seguinte. Trocar o
id na URL invalida a assinatura.

A preocupação de expor cliente foi resolvida por subtração: a tela de campo
mostra **menos** que a aba. Mapa, caixas, cabo candidato e contagem — **sem
nome, documento ou telefone**. Quem está na rua precisa saber onde cavar, não
quem mora ali. Um teste trava isso.

A mensagem passou a levar **dois links**, que respondem perguntas diferentes: o
da massiva mostra o mapa com cabo e caixas desenhados (é o que se olha ao
chegar), e o do Google Maps é o que abre o GPS do carro.

---

## Depois do plano: três pedidos que chegaram no meio

**Insight na parede quando não há massiva.** Cada um é um número medido da
operação com a pergunta que ele levanta — reincidência de trecho, quanto das
massivas tem causa registrada, duração típica do reparo, cobertura da planta.
Nada de dinheiro: MRR e meta mudam a audiência da tela e a operação para de
olhar. Amostra pequena não vira insight; parede com número fraco ensina a sala a
desconfiar da tela.

**Ação pelo controle da TV.** OK marca "estou tratando"; as teclas 1 a 5 dizem a
causa. Quem age é o **aparelho**, não uma pessoa — grava-se "marcado na TV da
bancada" em vez de inventar autor. O Enter deixou de avançar slide: uma tecla
com dois significados erraria os dois.

**Botões do card empilhados**, porque na coluna estreita da direita a fileira
horizontal esticava o card e sobrava espaço vazio.

---

## T7 — O mapa primeiro, com ícone e filtro

Pedido do Paulo na tela `/operations/massivas/30/#mapa`: *"deixa o mapa em
primeiro, e ainda parece que tem muita informação. Conseguimos adicionar ícones
no mapa? Casa para clientes, um ícone para CTO, um outro ícone para outras
coisas, e pintar eles? E também adicionar filtros — quero reduzir ruído,
aumentar foco. Onde o técnico tem que ir."*

**Ordem.** O detalhe abria com meia tela de análise antes do mapa. Quem abre
essa página durante um evento quer ver onde é; o raciocínio é leitura de depois.
O cabeçalho virou uma faixa (título, horário, quantos ainda estão fora e o
veredito) e tudo que explica *como o sistema chegou naquele escopo* —
vizinhança, reincidência, cabos candidatos, ressalvas, confiança, MRR e o
carimbo do poll — desceu para um bloco que abre sob demanda. Nada foi apagado:
recolher e remover são coisas diferentes, e a segunda apagaria as ressalvas que
seguram as afirmações da tela.

**Ícones.** Casa (⌂) para cliente, quadrado (■) para caixa, losango (◆) para
emenda, estrela (★) para POP — cada um por cima da bolinha colorida, que
continua sendo o que se lê de longe.

*Por que não emoji.* O motor por baixo do mapa é o MapLibre, que desenha texto
com os glifos servidos pelo style e só entende pontos de código até U+FFFF. Uma
casinha emoji (U+1F3E0) sairia como espaço em branco. E havia uma segunda
armadilha, encontrada com o mapa em branco na tela e a aba de rede aberta: o
Plotly pede a fonte "Open Sans", que o OpenFreeMap **não serve** — `404` em
`/fonts/Open%20Sans%20Regular/8960-9215.pbf` e nenhum texto. O desenho só
apareceu depois de declarar a fonte do próprio basemap (`Noto Sans Regular`).
Dois testes travam os dois: nenhum ícone acima de U+FFFF, e todo traço com
grupo.

**Filtros.** Cada camada virou caixa de marcar acima do mapa; o grupo viaja no
`meta.grupo` de cada traço e o JS liga/desliga com `Plotly.restyle`, sem
redesenhar nem perder o zoom que a pessoa deu. A legenda do Plotly saiu — ela
repetia as caixas e ficava deitada sobre o canto do mapa.

Dois padrões: **POP e ligação lógica começam desligados** (linhas longas que
cruzam o mapa e nunca são destino de ninguém — e, fora do enquadramento,
deixaram de encolher o evento a um punhado de pixels), e o botão **"foco no
técnico"**, que é o pedido dito por inteiro: fica só caixa e cabo, que é para
onde o carro vai.
