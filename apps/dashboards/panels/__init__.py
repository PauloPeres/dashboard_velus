"""Painéis de parede — a casca comum a todos eles.

**Vão existir dois painéis** (decisão de 18/09/2026, `docs/massivas-painel-tv-plano.md`):
o de NOC, que responde "está tudo bem? se não, onde?", e o executivo, em rota
separada. Nunca uma tela híbrida — a audiência é outra e a tela vira enfeite para
as duas.

Por isso **nada aqui é específico de um painel**. Pareamento, credencial de
display, endpoint de snapshot, rotação, barra fixa e as regras de frescor nascem
aqui, uma vez; cada painel entrega apenas o **conteúdo**: como montar o seu
snapshot e quais slides mostrar. Quando o executivo chegar, ele registra um
`PanelSpec` e herda toda a casca — sem copiar arquivo e sem bifurcar o
comportamento de frescor, que é justamente o que não pode divergir entre telas.

Um `PanelSpec` é o contrato:

- `key` entra na URL (`/paineis/noc/`) e na credencial do dispositivo: uma TV
  pareada para o NOC **não** abre o painel executivo;
- `snapshot` devolve o dicionário que a tela consome, e é a única coisa que bate
  no banco. A rotação de slides nunca consulta nada: ela redesenha o que já veio;
- `slides` são os templates, na ordem da rotação;
- `access_key` é a aba do catálogo de páginas de que o painel herda permissão,
  para quem o abre logado.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any


@dataclass(frozen=True)
class SlideSpec:
    """Um slide da rotação.

    `only_when` decide se o slide entra na rodada **desta** volta, olhando o
    snapshot: o mapa, por exemplo, só aparece quando há massiva aberta. Slide que
    não tem o que dizer sai da rotação em vez de mostrar tela vazia — numa TV,
    slide vazio treina a sala a ignorar a tela.
    """

    key: str
    title: str
    template: str
    seconds: int = 15
    only_when: Callable[[dict[str, Any]], bool] | None = None

    def should_show(self, snapshot: dict[str, Any]) -> bool:
        return True if self.only_when is None else bool(self.only_when(snapshot))


@dataclass(frozen=True)
class PanelSpec:
    """Um painel de parede: quem é, o que mostra e de onde tira o dado."""

    key: str
    title: str
    snapshot: Callable[[Any, datetime], dict[str, Any]]
    slides: tuple[SlideSpec, ...]
    access_key: str
    # Quanto tempo o snapshot pode ser reaproveitado entre requisições. Curto de
    # propósito: a TV atualiza sozinha o tempo todo, e sem cache cada volta da
    # rotação viraria carga no banco e no IXC. Mas cache longo é a forma mais
    # fácil de um painel mentir — ver `frescor` no shell.
    cache_seconds: int = 20
    subtitle: str = ""
    extras: dict[str, Any] = field(default_factory=dict)

    def visible_slides(self, snapshot: dict[str, Any]) -> list[SlideSpec]:
        return [s for s in self.slides if s.should_show(snapshot)]


_REGISTRY: dict[str, PanelSpec] = {}


def register(spec: PanelSpec) -> PanelSpec:
    """Registra um painel. Chamado no import do módulo de cada painel."""
    _REGISTRY[spec.key] = spec
    return spec


def get_panel(key: str) -> PanelSpec | None:
    _load_panels()
    return _REGISTRY.get(key)


def all_panels() -> list[PanelSpec]:
    _load_panels()
    return sorted(_REGISTRY.values(), key=lambda p: p.key)


def _load_panels() -> None:
    """Importa os módulos de painel na primeira consulta.

    Import tardio porque o painel importa agregações, que importam models — e
    este pacote é importado por `urls.py`, que roda antes do app registry ficar
    pronto.
    """
    if _REGISTRY:
        return
    from . import noc  # noqa: F401
