"""Testes do menu lateral gerado a partir do catálogo (`pages.nav_tree`).

Existe por um bug real: a página de Mensagens & Canais foi registrada em
`pages.py` — que se descrevia como "fonte única pro nav" — e mesmo assim não
apareceu no menu, porque o nav era uma segunda lista escrita à mão em
`base.html`. A correção foi tornar a frase verdadeira; estes testes são o que
impede a duplicação de voltar.

O que travam:

- toda página do catálogo aparece no menu (a regressão original);
- a ordem do menu é a ordem declarada, com o grupo recolhível na posição do seu
  primeiro item;
- o RBAC filtra o menu, e grupo sem item visível some junto;
- rota de detalhe acende a aba pai;
- `base.html` não voltou a escrever link de página na mão.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from django.urls import NoReverseMatch, reverse

from apps.dashboards.pages import PAGES, nav_tree

_BASE_HTML = (
    Path(__file__).resolve().parent.parent
    / "apps"
    / "dashboards"
    / "templates"
    / "dashboards"
    / "base.html"
)


def _links(arvore: list[dict]) -> list[str]:
    """Rótulos de todos os links da árvore, na ordem em que aparecem."""
    out: list[str] = []
    for no in arvore:
        itens = no["itens"] if no["tipo"] == "grupo" else [no]
        for item in itens:
            out.extend(link["label"] for link in item["links"])
    return out


def _item(arvore: list[dict], key: str) -> dict:
    """Item do menu pela chave, esteja ele solto ou dentro de um grupo."""
    for no in arvore:
        itens = no["itens"] if no["tipo"] == "grupo" else [no]
        for item in itens:
            if item["key"] == key:
                return item
    raise AssertionError(f"item '{key}' não está no menu")


def _chaves(arvore: list[dict]) -> list[str]:
    out: list[str] = []
    for no in arvore:
        if no["tipo"] == "grupo":
            out.extend(item["key"] for item in no["itens"])
        else:
            out.append(no["key"])
    return out


@pytest.fixture
def arvore_completa() -> list[dict]:
    return nav_tree(allowed_all=True, allowed_pages=set(), url_name_atual="executive")


@pytest.mark.django_db
class TestCatalogo:
    @pytest.mark.parametrize("page", PAGES, ids=lambda p: p["key"])
    def test_page_url_resolves(self, page: dict) -> None:
        """Rota declarada no catálogo existe de verdade."""
        try:
            reverse(page["url"])
        except NoReverseMatch:  # pragma: no cover - só falha se o catálogo quebrar
            pytest.fail(
                f"A página '{page['key']}' declara a rota '{page['url']}', que não "
                f"existe. Sem isso ela some do menu silenciosamente."
            )

    @pytest.mark.parametrize("page", PAGES, ids=lambda p: p["key"])
    def test_every_page_is_in_the_menu(
        self, page: dict, arvore_completa: list[dict]
    ) -> None:
        """A regressão original: página registrada tem que aparecer no menu."""
        assert page["key"] in _chaves(arvore_completa)

    def test_mensagens_e_canais_no_menu(self, arvore_completa: list[dict]) -> None:
        assert "Mensagens & Canais" in _links(arvore_completa)


@pytest.mark.django_db
class TestOrdemEAgrupamento:
    def test_menu_order_follows_catalog_order(
        self, arvore_completa: list[dict]
    ) -> None:
        assert _chaves(arvore_completa) == [p["key"] for p in PAGES]

    def test_group_appears_once_at_first_member_position(
        self, arvore_completa: list[dict]
    ) -> None:
        grupos = [no for no in arvore_completa if no["tipo"] == "grupo"]
        assert len(grupos) == 1
        assert grupos[0]["label"] == "Despesas"
        # O grupo ocupa o lugar do primeiro item dele (Caixa), logo depois de
        # Inadimplência — é o que mantém o menu na ordem declarada.
        posicoes = [
            no["key"] if no["tipo"] == "link" else "GRUPO" for no in arvore_completa
        ]
        assert posicoes[posicoes.index("GRUPO") - 1] == "financial"

    def test_dre_carries_its_sibling_link(self, arvore_completa: list[dict]) -> None:
        """DRE tem dois links no menu dividindo a MESMA chave de acesso."""
        rotulos = _links(arvore_completa)
        assert "DRE Gerencial" in rotulos
        assert "Fluxo de Caixa por Conta" in rotulos

    def test_nav_label_overrides_catalog_label(
        self, arvore_completa: list[dict]
    ) -> None:
        """O menu usa 'Fluxo de Caixa'; a tela de permissões continua com 'Caixa'."""
        assert "Fluxo de Caixa" in _links(arvore_completa)
        assert next(p for p in PAGES if p["key"] == "cashflow")["label"] == "Caixa"

    def test_group_opens_when_current_page_is_inside(self) -> None:
        arvore = nav_tree(
            allowed_all=True, allowed_pages=set(), url_name_atual="compromissos"
        )
        grupo = next(no for no in arvore if no["tipo"] == "grupo")
        assert grupo["aberto"] is True

    def test_group_closed_when_current_page_is_outside(
        self, arvore_completa: list[dict]
    ) -> None:
        grupo = next(no for no in arvore_completa if no["tipo"] == "grupo")
        assert grupo["aberto"] is False

    def test_group_opens_on_a_sibling_link(self) -> None:
        """Estando no Fluxo de Caixa por Conta, o grupo abre e o DRE acende."""
        arvore = nav_tree(
            allowed_all=True, allowed_pages=set(), url_name_atual="dre_detalhe"
        )
        grupo = next(no for no in arvore if no["tipo"] == "grupo")
        assert grupo["aberto"] is True
        dre = next(item for item in grupo["itens"] if item["key"] == "dre")
        assert dre["ativo"] is True


@pytest.mark.django_db
class TestPermissoes:
    def test_menu_limited_to_allowed_pages(self) -> None:
        arvore = nav_tree(
            allowed_all=False,
            allowed_pages={"mensagens", "atendimento"},
            url_name_atual="mensagens",
        )
        assert _chaves(arvore) == ["atendimento", "mensagens"]

    def test_group_disappears_when_no_member_is_allowed(self) -> None:
        arvore = nav_tree(
            allowed_all=False, allowed_pages={"mensagens"}, url_name_atual="mensagens"
        )
        assert all(no["tipo"] != "grupo" for no in arvore)

    def test_group_survives_with_a_single_allowed_member(self) -> None:
        arvore = nav_tree(
            allowed_all=False, allowed_pages={"burn"}, url_name_atual="burn"
        )
        grupo = next(no for no in arvore if no["tipo"] == "grupo")
        assert [item["key"] for item in grupo["itens"]] == ["burn"]

    def test_empty_permissions_render_an_empty_menu(self) -> None:
        assert nav_tree(
            allowed_all=False, allowed_pages=set(), url_name_atual=None
        ) == []


@pytest.mark.django_db
class TestPaginaAtiva:
    def test_current_page_is_marked_active(self) -> None:
        arvore = nav_tree(
            allowed_all=True, allowed_pages=set(), url_name_atual="mensagens"
        )
        assert _item(arvore, "mensagens")["ativo"] is True

    @pytest.mark.parametrize(
        ("rota_detalhe", "chave_pai"),
        [
            ("customer_detail", "customers"),
            ("atendimento_detail", "conversas_ruins"),
        ],
    )
    def test_detail_route_lights_up_parent_tab(
        self, rota_detalhe: str, chave_pai: str
    ) -> None:
        """Rota de detalhe não tem item próprio — acende a aba de onde veio."""
        arvore = nav_tree(
            allowed_all=True, allowed_pages=set(), url_name_atual=rota_detalhe
        )
        assert _item(arvore, chave_pai)["ativo"] is True


class TestSemDuplicacaoNoTemplate:
    def test_base_html_does_not_hardcode_page_links(self) -> None:
        """`base.html` não pode voltar a escrever link de página na mão.

        As exceções são as rotas que não são "abas": home (logo) e configurações
        (rodapé do usuário), que não estão no catálogo.
        """
        html = _BASE_HTML.read_text(encoding="utf-8")
        permitidas = {"dashboards:home", "dashboards:settings", "account_logout"}
        hardcoded = {
            page["url"]
            for page in PAGES
            if "{% url '" + page["url"] + "'" in html
        }
        assert not hardcoded, (
            f"base.html voltou a escrever links de página na mão: {sorted(hardcoded)}. "
            f"O menu deve sair de pages.PAGES via o include _sidebar_nav.html. "
            f"(Só {sorted(permitidas)} podem aparecer lá.)"
        )

    def test_base_html_includes_the_generated_nav(self) -> None:
        html = _BASE_HTML.read_text(encoding="utf-8")
        assert 'include "dashboards/_sidebar_nav.html"' in html
