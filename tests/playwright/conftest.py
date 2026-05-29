"""Fixtures Playwright para testes de browser do Velus Dashboard.

Execução:
    uv run pytest tests/playwright/ --base-url=http://localhost:8000 -v

Pré-requisitos:
    - Docker containers rodando (docker compose up -d)
    - Usuário playwright@velus.test criado com membership em 'velus'
      (criado automaticamente pela fixture setup_playwright_user se não existir)
"""

from __future__ import annotations

import pytest
from playwright.sync_api import Page, expect

BASE_URL = "http://localhost:8000"
TEST_EMAIL = "playwright@velus.test"
TEST_PASSWORD = "playwright-test-2026"


@pytest.fixture(scope="session")
def base_url() -> str:
    return BASE_URL


@pytest.fixture
def login_page(page: Page) -> Page:
    """Navega para a página de login e retorna a page."""
    page.goto(f"{BASE_URL}/accounts/login/")
    expect(page).to_have_title("Entrar · Velus Dashboard")
    return page


@pytest.fixture
def authenticated_page(page: Page) -> Page:
    """Faz login e retorna page já autenticada no dashboard."""
    page.goto(f"{BASE_URL}/accounts/login/")
    page.fill('input[name="login"]', TEST_EMAIL)
    page.fill('input[name="password"]', TEST_PASSWORD)
    page.click('button[type="submit"]')
    # Aguarda redirect para algum dashboard
    page.wait_for_url(f"{BASE_URL}/executive/", timeout=10_000)
    return page
