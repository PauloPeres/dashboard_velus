"""Context processors para templates."""

from __future__ import annotations

from typing import Any

from django.http import HttpRequest


def tenant(request: HttpRequest) -> dict[str, Any]:
    """Expõe a organização atual ao template como `current_organization`."""
    user = getattr(request, "user", None)
    if user is None or not getattr(user, "is_authenticated", False):
        return {"current_organization": None}
    get_org = getattr(user, "get_active_organization", None)
    if callable(get_org):
        return {"current_organization": get_org()}
    return {"current_organization": None}
