"""Testes do servidor MCP read-only (#45).

Cobre o model/auth de token (McpToken + authenticate_token), o registro de
ferramentas (org vem do contexto, saída JSON-safe) e o middleware Bearer ASGI
(health sem auth, 401 sem token, injeção de org no contexto com token válido).
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

from apps.mcp.models import McpToken, _hash_token, authenticate_token
from apps.mcp.registry import _json_safe, _normalize, _require_org
from apps.mcp.server import BearerTenantMiddleware
from apps.shared.context import (
    get_current_organization,
    reset_current_organization,
    set_current_organization,
)
from apps.shared.exceptions import NoOrganizationInContextError
from apps.tenancy.models import Organization


@pytest.mark.django_db
class TestMcpToken:
    def test_issue_returns_raw_and_persists_only_hash(
        self, organization_a: Organization
    ) -> None:
        token, raw = McpToken.issue(organization=organization_a, name="Claude")
        assert raw.startswith("velus_mcp_")
        assert token.token_hash == _hash_token(raw)
        assert token.token_hash != raw
        assert raw.startswith(token.prefix)
        assert token.is_active is True

    def test_authenticate_valid_token_returns_org(
        self, organization_a: Organization
    ) -> None:
        _, raw = McpToken.issue(organization=organization_a, name="x")
        org = authenticate_token(raw)
        assert org is not None
        assert org.id == organization_a.id

    def test_authenticate_updates_last_used(
        self, organization_a: Organization
    ) -> None:
        token, raw = McpToken.issue(organization=organization_a, name="x")
        assert token.last_used_at is None
        authenticate_token(raw)
        token.refresh_from_db()
        assert token.last_used_at is not None

    def test_authenticate_wrong_token_returns_none(
        self, organization_a: Organization
    ) -> None:
        McpToken.issue(organization=organization_a, name="x")
        assert authenticate_token("velus_mcp_naoexiste") is None

    def test_authenticate_inactive_token_returns_none(
        self, organization_a: Organization
    ) -> None:
        token, raw = McpToken.issue(organization=organization_a, name="x")
        token.is_active = False
        token.save(update_fields=["is_active"])
        assert authenticate_token(raw) is None

    def test_authenticate_empty_returns_none(self) -> None:
        assert authenticate_token(None) is None
        assert authenticate_token("") is None


class TestSerialization:
    def test_json_safe_handles_decimal_and_date(self) -> None:
        assert _json_safe(Decimal("1.5")) == 1.5
        assert _json_safe(date(2026, 1, 2)) == "2026-01-02"

    def test_json_safe_rejects_unknown(self) -> None:
        with pytest.raises(TypeError):
            _json_safe(object())

    def test_normalize_roundtrips(self) -> None:
        out = _normalize({"mrr": Decimal("10.00"), "dia": date(2026, 1, 1)})
        assert out == {"mrr": 10.0, "dia": "2026-01-01"}


@pytest.mark.django_db
class TestRegistryContext:
    def test_require_org_raises_without_context(self) -> None:
        token = set_current_organization(None)
        try:
            with pytest.raises(NoOrganizationInContextError):
                _require_org()
        finally:
            reset_current_organization(token)

    def test_kpis_handler_returns_json_safe(
        self, organization_a: Organization
    ) -> None:
        from apps.mcp.registry import TOOL_SPECS

        spec = next(s for s in TOOL_SPECS if s.name == "contratos_kpis")
        token = set_current_organization(organization_a)
        try:
            out = spec.handler()
        finally:
            reset_current_organization(token)
        assert isinstance(out, dict)
        assert "mrr_now" in out


async def _fake_downstream(captured: dict):
    async def app(scope, receive, send) -> None:
        captured["org"] = get_current_organization()
        await send(
            {"type": "http.response.start", "status": 200, "headers": []}
        )
        await send({"type": "http.response.body", "body": b"ok"})

    return app


def _http_scope(path: str, *, auth: str | None = None):
    headers = []
    if auth is not None:
        headers.append((b"authorization", auth.encode()))
    return {"type": "http", "path": path, "headers": headers}


async def _collect(app, scope) -> tuple[int, bytes]:
    sent: list[dict] = []

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(msg):
        sent.append(msg)

    await app(scope, receive, send)
    status = next(m["status"] for m in sent if m["type"] == "http.response.start")
    body = b"".join(m.get("body", b"") for m in sent if m["type"] == "http.response.body")
    return status, body


class TestBearerMiddleware:
    """Auth do middleware ASGI — autenticação real é stubada (já coberta por
    TestMcpToken) para manter estes testes async hermético, sem tocar o banco."""

    async def test_health_is_public(self) -> None:
        captured: dict = {}
        mw = BearerTenantMiddleware(await _fake_downstream(captured))
        status, body = await _collect(mw, _http_scope("/healthz"))
        assert status == 200
        assert b"ok" in body
        # downstream NÃO deve ter sido chamado
        assert "org" not in captured

    async def test_missing_token_is_401(self, monkeypatch) -> None:
        monkeypatch.setattr("apps.mcp.models.authenticate_token", lambda raw: None)
        captured: dict = {}
        mw = BearerTenantMiddleware(await _fake_downstream(captured))
        status, _ = await _collect(mw, _http_scope("/mcp"))
        assert status == 401
        assert "org" not in captured

    async def test_invalid_token_is_401(self, monkeypatch) -> None:
        monkeypatch.setattr("apps.mcp.models.authenticate_token", lambda raw: None)
        captured: dict = {}
        mw = BearerTenantMiddleware(await _fake_downstream(captured))
        status, _ = await _collect(
            mw, _http_scope("/mcp", auth="Bearer velus_mcp_nope")
        )
        assert status == 401
        assert "org" not in captured

    async def test_valid_token_injects_org(self, monkeypatch) -> None:
        sentinel = Organization(id=4242, slug="acme", name="ACME ISP")
        seen: dict = {}

        def _auth(raw):
            seen["raw"] = raw
            return sentinel

        monkeypatch.setattr("apps.mcp.models.authenticate_token", _auth)
        captured: dict = {}
        mw = BearerTenantMiddleware(await _fake_downstream(captured))
        status, _ = await _collect(mw, _http_scope("/mcp", auth="Bearer velus_mcp_ok"))
        assert status == 200
        assert seen["raw"] == "velus_mcp_ok"
        assert captured["org"] is sentinel
