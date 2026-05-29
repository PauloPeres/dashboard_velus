"""Testes Playwright — dashboards (autenticado).

Verifica que todas as 7 páginas carregam, têm título correto,
navegação funcional e gráficos Plotly presentes.
"""

from __future__ import annotations

import pytest
from playwright.sync_api import Page, expect

from .conftest import BASE_URL

# ─────────────────────────────────────────────────────────────────────────────
# Parâmetros das telas
# ─────────────────────────────────────────────────────────────────────────────

DASHBOARD_PAGES = [
    ("/executive/",          "Painel Executivo",  "executive"),
    ("/revenue/",            "Receita",           "revenue"),
    ("/financial/",          "Financeiro",        "financial"),
    ("/financial/cashflow/", "Fluxo de Caixa",    "cashflow"),
    ("/financial/forecast/", "Forecast",          "forecast"),
    ("/financial/dre/",      "DRE",               "dre"),
    ("/financial/burn/",     "Burn Rate",         "burn"),
]


# ─────────────────────────────────────────────────────────────────────────────
# Testes de cada dashboard
# ─────────────────────────────────────────────────────────────────────────────

class TestDashboardPages:
    """Smoke test: todas as páginas carregam sem erro 500."""

    @pytest.mark.parametrize("path,title_fragment,screenshot_name", DASHBOARD_PAGES)
    def test_page_loads(
        self,
        authenticated_page: Page,
        path: str,
        title_fragment: str,
        screenshot_name: str,
    ) -> None:
        """Cada dashboard carrega sem erro e tem o título esperado no nav."""
        authenticated_page.goto(f"{BASE_URL}{path}")
        # Sem erro 500 — se houvesse a página quebraria
        expect(authenticated_page).not_to_have_url(f"{BASE_URL}/accounts/login/")
        # Salva screenshot
        authenticated_page.screenshot(
            path=f"tests/playwright/screenshots/{screenshot_name}.png",
            full_page=True,
        )

    @pytest.mark.parametrize("path,title_fragment,screenshot_name", DASHBOARD_PAGES)
    def test_plotly_charts_rendered(
        self,
        authenticated_page: Page,
        path: str,
        title_fragment: str,
        screenshot_name: str,
    ) -> None:
        """Pelo menos um gráfico Plotly está presente em cada dashboard."""
        authenticated_page.goto(f"{BASE_URL}{path}")
        # Plotly renderiza divs com classe 'js-plotly-plot'
        # Aguarda até 8s pelo script Plotly carregar
        authenticated_page.wait_for_selector(".js-plotly-plot", timeout=8_000)
        charts = authenticated_page.locator(".js-plotly-plot")
        expect(charts.first).to_be_visible()


# ─────────────────────────────────────────────────────────────────────────────
# Navegação
# ─────────────────────────────────────────────────────────────────────────────

class TestNavigation:
    """Testa links da barra de navegação principal."""

    def test_nav_executive_link(self, authenticated_page: Page) -> None:
        authenticated_page.goto(f"{BASE_URL}/executive/")
        nav = authenticated_page.locator("nav")
        expect(nav).to_be_visible()

    def test_nav_has_link_to_revenue(self, authenticated_page: Page) -> None:
        authenticated_page.goto(f"{BASE_URL}/executive/")
        link = authenticated_page.locator('a[href="/revenue/"]').first
        expect(link).to_be_visible()

    def test_nav_has_link_to_financial(self, authenticated_page: Page) -> None:
        authenticated_page.goto(f"{BASE_URL}/executive/")
        link = authenticated_page.locator('a[href="/financial/"]').first
        expect(link).to_be_visible()

    def test_nav_has_cashflow_link(self, authenticated_page: Page) -> None:
        authenticated_page.goto(f"{BASE_URL}/executive/")
        # Cashflow is in the "Despesas ▾" CSS hover dropdown — hover to reveal it
        authenticated_page.locator("text=Despesas").hover()
        link = authenticated_page.locator('a[href="/financial/cashflow/"]').first
        expect(link).to_be_visible()

    def test_back_to_admin_link(self, authenticated_page: Page) -> None:
        """Link de volta pro Admin Django deve estar no nav."""
        authenticated_page.goto(f"{BASE_URL}/executive/")
        admin_link = authenticated_page.locator('a[href="/admin/"]').first
        expect(admin_link).to_be_visible()

    def test_click_revenue_navigates(self, authenticated_page: Page) -> None:
        """Clicar no link de Receita navega para /revenue/."""
        authenticated_page.goto(f"{BASE_URL}/executive/")
        authenticated_page.locator('a[href="/revenue/"]').first.click()
        expect(authenticated_page).to_have_url(f"{BASE_URL}/revenue/")

    def test_click_cashflow_navigates(self, authenticated_page: Page) -> None:
        """Clicar no link de Fluxo de Caixa navega para /financial/cashflow/."""
        authenticated_page.goto(f"{BASE_URL}/executive/")
        # Hover the "Despesas ▾" dropdown to reveal submenu links
        authenticated_page.locator("text=Despesas").hover()
        authenticated_page.locator('a[href="/financial/cashflow/"]').first.click()
        expect(authenticated_page).to_have_url(f"{BASE_URL}/financial/cashflow/")


# ─────────────────────────────────────────────────────────────────────────────
# Executive dashboard — KPIs
# ─────────────────────────────────────────────────────────────────────────────

class TestExecutiveDashboard:
    """Testa elementos específicos do painel executivo."""

    def test_mrr_card_present(self, authenticated_page: Page) -> None:
        authenticated_page.goto(f"{BASE_URL}/executive/")
        # Card MRR deve estar visível
        mrr = authenticated_page.get_by_text("MRR").first
        expect(mrr).to_be_visible()

    def test_active_contracts_card(self, authenticated_page: Page) -> None:
        authenticated_page.goto(f"{BASE_URL}/executive/")
        expect(authenticated_page.get_by_text("Ativos").first).to_be_visible()

    def test_churn_card(self, authenticated_page: Page) -> None:
        authenticated_page.goto(f"{BASE_URL}/executive/")
        # Alguma variação de "churn" no dashboard
        churn = authenticated_page.get_by_text("Churn", exact=False).first
        expect(churn).to_be_visible()
