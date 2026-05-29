"""Root URL configuration."""

from __future__ import annotations

from django.conf import settings
from django.contrib import admin
from django.http import HttpResponse
from django.urls import include, path
from django.views.decorators.csrf import csrf_exempt


@csrf_exempt
def health(_request):
    """Liveness/readiness probe — 200 sem tocar DB/auth. Usado pelos probes do K8s."""
    return HttpResponse("ok", content_type="text/plain")


urlpatterns = [
    path("health/", health, name="health"),
    path("admin/", admin.site.urls),
    path("accounts/", include("allauth.urls")),
    path("scenarios/", include("apps.scenarios.urls")),
    path("sync/", include("apps.sync.urls")),
    path("", include("apps.dashboards.urls")),
]

if settings.DEBUG:
    import debug_toolbar

    urlpatterns += [
        path("__debug__/", include(debug_toolbar.urls)),
    ]
