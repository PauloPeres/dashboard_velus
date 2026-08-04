"""Ad-hoc mobile screenshot capture (390x844, iPhone-ish). Render-only, not committed."""
import sys
from playwright.sync_api import sync_playwright

BASE = "http://localhost:8088"
EMAIL = "playwright@velus.test"
PWD = "playwright-test-2026"
OUT = "/app/_mobile_shots/live"

PAGES = [
    ("executive", "/executive/"),
    ("revenue", "/revenue/"),
    ("contracts", "/contracts/"),
    ("financial", "/financial/"),
    ("forecast", "/forecast/"),
    ("dre", "/dre/"),
    ("churn", "/churn/"),
    ("risk", "/risk/"),
    ("operations", "/operations/"),
    ("network", "/network/"),
    ("tecnicos", "/tecnicos/"),
    ("compromissos", "/compromissos/"),
    ("descasamento", "/descasamento/"),
    ("customers", "/customers/"),
]

import os
os.makedirs(OUT, exist_ok=True)

with sync_playwright() as p:
    browser = p.chromium.launch()
    ctx = browser.new_context(viewport={"width": 390, "height": 844}, device_scale_factor=2)
    page = ctx.new_page()
    page.goto(f"{BASE}/accounts/login/")
    page.fill('input[name="login"]', EMAIL)
    page.fill('input[name="password"]', PWD)
    page.click('button[type="submit"]')
    page.wait_for_url(f"{BASE}/", timeout=15000)
    for name, path in PAGES:
        try:
            page.goto(f"{BASE}{path}", wait_until="networkidle", timeout=20000)
            page.wait_for_timeout(1200)  # let Plotly render
            page.screenshot(path=f"{OUT}/{name}.png", full_page=True)
            print(f"OK {name}")
        except Exception as e:
            print(f"ERR {name}: {e}")
    browser.close()
