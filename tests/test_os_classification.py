"""Testes do classificador de assuntos de OS (#32).

`classify_subject` mapeia nomes de assunto IXC (org-específicos) em categorias.
Só a categoria SUPPORT conta pro sinal de chamados frequentes do churn —
rotinas (instalação, equipamento, financeiro, titularidade) são excluídas.
"""

from __future__ import annotations

import pytest

from apps.helpdesk.application.os_classification import (
    COMMERCIAL,
    EQUIPMENT,
    FINANCE,
    INSTALL,
    LIFECYCLE,
    OTHER,
    SUPPORT,
    churn_relevant_subject_ids,
    classify_subject,
)


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        # Suporte / problema técnico — relevante p/ churn
        ("Manutenção Técnica", SUPPORT),
        ("Manutenção na Rede", SUPPORT),
        ("Acesso ao Roteador", SUPPORT),
        ("Tubulação Obstruída", SUPPORT),
        ("Visita técnica gerada", SUPPORT),
        ("Atendimento ao Cliente", SUPPORT),
        ("ESTRUTURA - Queda em Massa", SUPPORT),
        ("Regularização de Rede", SUPPORT),
        ("Verificação de rede", SUPPORT),
        # Instalação / onboarding — rotina
        ("Nova Instalação", INSTALL),
        ("Agendar Instalação de Roteador Adicional", INSTALL),
        ("Ativação de cliente", INSTALL),
        ("REINSTALAÇÃO DE CLIENTES", INSTALL),
        ("Verificação de Viabilidade de instalação", INSTALL),
        ("construção de rede", INSTALL),
        # Equipamento — rotina
        ("Retirada de Equipamentos", EQUIPMENT),
        ("Troca de equipamento", EQUIPMENT),
        ("Mudança de Cômodo", EQUIPMENT),
        # Financeiro — rotina
        ("Ajustar boleto", FINANCE),
        ("Cobrança Manual", FINANCE),
        ("Desbloqueio de Confiança", FINANCE),
        ("Inadimplência", FINANCE),
        # Comercial — rotina
        ("Alteração de Plano", COMMERCIAL),
        ("Mudança de Pacote", COMMERCIAL),
        # "Upgrade com mudança de equipamento" casa EQUIPMENT antes — também rotina.
        ("Upgrade com mudança de equipamento", EQUIPMENT),
        # Ciclo de vida (cancelamento/retenção) — não conta no sinal de suporte
        ("Cancelamento", LIFECYCLE),
        ("Retenção cliente", LIFECYCLE),
        ("Cliente Desistiu", LIFECYCLE),
        ("DESATIVAR CONTRATO", LIFECYCLE),
        # Desconhecido
        ("Endereço Fraude", OTHER),
        ("", OTHER),
    ],
)
def test_classify_subject(name: str, expected: str) -> None:
    assert classify_subject(name) == expected


class TestChurnRelevantSubjectIds:
    def test_empty_map_returns_none(self) -> None:
        # Sem lookups → None sinaliza fallback (contar todos os chamados).
        assert churn_relevant_subject_ids({}) is None

    def test_filters_to_support_only(self) -> None:
        subject_map = {
            "1": "Nova Instalação",
            "2": "Manutenção Técnica",
            "55": "Cobrança",
            "58": "Acesso ao Roteador",
            "45": "Cancelamento",
        }
        assert churn_relevant_subject_ids(subject_map) == {"2", "58"}
