"""Testes Playwright — página de login.

Verifica layout, elementos visuais, validação de formulário e fluxo de autenticação.
"""

from __future__ import annotations

import pytest
from playwright.sync_api import Page, expect

from .conftest import BASE_URL, TEST_EMAIL, TEST_PASSWORD

# ─────────────────────────────────────────────────────────────────────────────
# Layout e estrutura visual
# ─────────────────────────────────────────────────────────────────────────────

class TestLoginLayout:
    """Garante que o split-layout está renderizado corretamente."""

    def test_page_title(self, login_page: Page) -> None:
        expect(login_page).to_have_title("Entrar · Velus Dashboard")

    def test_brand_panel_visible(self, login_page: Page) -> None:
        """Painel esquerdo escuro com identidade da marca."""
        brand = login_page.locator(".brand-gradient")
        expect(brand).to_be_visible()

    def test_brand_name_velus(self, login_page: Page) -> None:
        """Nome 'Velus' aparece no painel de marca."""
        expect(login_page.get_by_text("Velus").first).to_be_visible()

    def test_tagline_visibilidade_total(self, login_page: Page) -> None:
        """Headline de produto está presente no painel."""
        expect(login_page.get_by_text("Visibilidade total")).to_be_visible()

    def test_metric_cards_present(self, login_page: Page) -> None:
        """Cards MRR, DRE e Churn estão no painel."""
        for label in ("MRR", "DRE", "Churn"):
            expect(login_page.get_by_text(label).first).to_be_visible()

    def test_form_heading(self, login_page: Page) -> None:
        """Cabeçalho do formulário."""
        expect(login_page.get_by_text("Bem-vindo de volta")).to_be_visible()

    def test_email_field_present(self, login_page: Page) -> None:
        field = login_page.locator('input[name="login"]')
        expect(field).to_be_visible()
        expect(field).to_have_attribute("type", "email")

    def test_password_field_present(self, login_page: Page) -> None:
        field = login_page.locator('input[name="password"]')
        expect(field).to_be_visible()
        expect(field).to_have_attribute("type", "password")

    def test_submit_button_text(self, login_page: Page) -> None:
        btn = login_page.locator('button[type="submit"]')
        expect(btn).to_be_visible()
        expect(btn).to_contain_text("Entrar no dashboard")

    def test_forgot_password_link(self, login_page: Page) -> None:
        link = login_page.get_by_text("Esqueceu a senha?")
        expect(link).to_be_visible()
        expect(link).to_have_attribute("href", "/accounts/password/reset/")

    def test_footer_text(self, login_page: Page) -> None:
        expect(login_page.get_by_text("Acesso restrito")).to_be_visible()

    def test_screenshot_login(self, login_page: Page) -> None:
        """Screenshot de referência da página de login."""
        login_page.screenshot(path="tests/playwright/screenshots/login.png", full_page=True)


# ─────────────────────────────────────────────────────────────────────────────
# Validação de formulário
# ─────────────────────────────────────────────────────────────────────────────

class TestLoginValidation:
    """Testa feedback de erro e comportamento com credenciais inválidas."""

    def test_empty_form_required_validation(self, page: Page) -> None:
        """Browser deve bloquear submit com campos vazios (HTML5 required)."""
        page.goto(f"{BASE_URL}/accounts/login/")
        # Tenta clicar submit sem preencher nada
        page.click('button[type="submit"]')
        # Página não muda (browser valida o required)
        expect(page).to_have_url(f"{BASE_URL}/accounts/login/")

    def test_wrong_password_shows_error(self, page: Page) -> None:
        """Senha errada retorna mensagem de erro do servidor."""
        page.goto(f"{BASE_URL}/accounts/login/")
        page.fill('input[name="login"]', TEST_EMAIL)
        page.fill('input[name="password"]', "senha-errada-xyz-123")
        page.click('button[type="submit"]')
        # Permanece na página de login
        expect(page).to_have_url(f"{BASE_URL}/accounts/login/")
        # Bloco de erro está visível (div com bg-red-50)
        error_block = page.locator(".bg-red-50")
        expect(error_block).to_be_visible()

    def test_unknown_email_shows_error(self, page: Page) -> None:
        """Email inexistente retorna mensagem de erro."""
        page.goto(f"{BASE_URL}/accounts/login/")
        page.fill('input[name="login"]', "ninguem@naoexiste.com")
        page.fill('input[name="password"]', "qualquersenha")
        page.click('button[type="submit"]')
        expect(page).to_have_url(f"{BASE_URL}/accounts/login/")
        expect(page.locator(".bg-red-50")).to_be_visible()


# ─────────────────────────────────────────────────────────────────────────────
# Fluxo de autenticação bem-sucedida
# ─────────────────────────────────────────────────────────────────────────────

class TestLoginFlow:
    """Testa o fluxo completo de login → redirect → dashboard."""

    def test_valid_login_redirects_to_executive(self, page: Page) -> None:
        """Login bem-sucedido redireciona para /executive/."""
        page.goto(f"{BASE_URL}/accounts/login/")
        page.fill('input[name="login"]', TEST_EMAIL)
        page.fill('input[name="password"]', TEST_PASSWORD)
        page.click('button[type="submit"]')
        page.wait_for_url(f"{BASE_URL}/executive/", timeout=10_000)
        expect(page).to_have_url(f"{BASE_URL}/executive/")

    def test_protected_route_redirects_to_login(self, page: Page) -> None:
        """Acessar dashboard sem login redireciona para a página de login."""
        page.goto(f"{BASE_URL}/executive/")
        expect(page).to_have_url(f"{BASE_URL}/accounts/login/?next=/executive/")

    def test_next_param_redirects_correctly(self, page: Page) -> None:
        """Login via ?next= redireciona para a página solicitada."""
        page.goto(f"{BASE_URL}/accounts/login/?next=/revenue/")
        page.fill('input[name="login"]', TEST_EMAIL)
        page.fill('input[name="password"]', TEST_PASSWORD)
        page.click('button[type="submit"]')
        page.wait_for_url(f"{BASE_URL}/revenue/", timeout=10_000)
        expect(page).to_have_url(f"{BASE_URL}/revenue/")
