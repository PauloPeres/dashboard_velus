"""URLs do bounded context dashboards."""

from __future__ import annotations

from django.urls import path

from . import views

app_name = "dashboards"

urlpatterns = [
    path("", views.home, name="home"),
    path("executive/", views.executive, name="executive"),
    path("revenue/", views.revenue, name="revenue"),
    path("contracts/", views.contracts, name="contracts"),
    path("financial/", views.financial, name="financial"),
    path("financial/cashflow/", views.cashflow, name="cashflow"),
    path("financial/forecast/", views.forecast, name="forecast"),
    path("financial/dre/", views.dre, name="dre"),
    path("financial/dre-contas/", views.dre_detalhe, name="dre_detalhe"),
    path("financial/burn/", views.burn, name="burn"),
    path("financial/pessoas/", views.pessoas, name="pessoas"),
    path("financial/compromissos/", views.compromissos, name="compromissos"),
    path("financial/descasamento/", views.descasamento, name="descasamento"),
    path("churn/", views.churn, name="churn"),
    path("risk/", views.risk, name="risk"),
    path("operations/", views.operations, name="operations"),
    path("operations/os/", views.os_dashboard, name="os_dashboard"),
    path("operations/tecnicos/", views.tecnicos, name="tecnicos"),
    path("operations/atendimento/", views.atendimento, name="atendimento"),
    path("operations/conversas-ruins/", views.conversas_ruins, name="conversas_ruins"),
    path("operations/qa/", views.qa_supervisor, name="qa_supervisor"),
    path(
        "operations/conversas-ruins/<int:atendimento_id>/",
        views.atendimento_detail,
        name="atendimento_detail",
    ),
    path("network/", views.network, name="network"),
    path("sales/", views.sales, name="sales"),
    path("customers/", views.customers, name="customers"),
    path("customers/<int:customer_id>/", views.customer_detail, name="customer_detail"),
    path("settings/", views.settings_view, name="settings"),
]
