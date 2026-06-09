"""Testes da IA supervisora de QA (LLM-as-judge) — issue #51.

Cobre redação de PII (pura), parse da resposta do juiz, `review_conversation`
com um juiz fake (zero rede) e o seletor de amostragem. Nenhum teste toca a API
Gemini — o cliente é injetado; só o parse da resposta do juiz é testado direto.
"""

from __future__ import annotations

import json
from typing import Any

import pytest
from django.utils import timezone

from apps.analytics.application.qa_review import (
    _parse_review,
    review_conversation,
    select_conversations_for_qa,
)
from apps.analytics.infrastructure.models import QAReview
from apps.atendimento.application.redaction import redact
from apps.atendimento.infrastructure.models import Atendimento, Mensagem
from apps.shared.context import set_current_organization
from apps.tenancy.models import Organization


# =============================================================================
# Redação de PII — função pura
# =============================================================================
class TestRedaction:
    def test_masks_cpf_formatted_and_raw(self) -> None:
        assert redact("meu cpf é 123.456.789-09") == "meu cpf é [CPF]"
        assert redact("doc 12345678909 ok") == "doc [CPF] ok"

    def test_masks_cnpj(self) -> None:
        assert "[CNPJ]" in redact("empresa 12.345.678/0001-99")

    def test_masks_phone_and_email(self) -> None:
        out = redact("ligue (15) 99999-8888 ou email joao@x.com")
        assert "[TELEFONE]" in out
        assert "[EMAIL]" in out

    def test_masks_known_name_and_document(self) -> None:
        out = redact(
            "Olá João Silva, confirma o 99988877766?",
            names=("João Silva",),
            documents=("99988877766",),
        )
        assert "João" not in out
        assert "Silva" not in out
        assert "99988877766" not in out
        assert "[NOME]" in out

    def test_empty_is_noop(self) -> None:
        assert redact("") == ""


# =============================================================================
# Parse da resposta do juiz
# =============================================================================
class TestParseReview:
    def test_parses_plain_json(self) -> None:
        data = _parse_review('{"tom": 4, "resolveu": true}')
        assert data["tom"] == 4
        assert data["resolveu"] is True

    def test_parses_json_with_code_fence_and_prose(self) -> None:
        text = 'Claro!\n```json\n{"overall_score": 80}\n```\nEspero ajudar.'
        assert _parse_review(text)["overall_score"] == 80

    def test_raises_without_json(self) -> None:
        with pytest.raises(ValueError, match="sem objeto JSON"):
            _parse_review("não consegui avaliar")


# =============================================================================
# Extração de texto da resposta do Gemini — função pura (sem rede)
# =============================================================================
class TestGeminiExtractText:
    def test_concatenates_candidate_parts(self) -> None:
        from apps.integrations.gemini.client import GeminiClient

        response = {
            "candidates": [
                {"content": {"parts": [{"text": '{"overall_score":'}, {"text": " 80}"}]}}
            ]
        }
        assert GeminiClient._extract_text(response) == '{"overall_score": 80}'

    def test_empty_on_malformed(self) -> None:
        from apps.integrations.gemini.client import GeminiClient

        assert GeminiClient._extract_text({}) == ""
        assert GeminiClient._extract_text("oops") == ""
        assert GeminiClient._extract_text({"candidates": [{}]}) == ""


# =============================================================================
# Helpers de fixture
# =============================================================================
def _atendimento(
    org: Organization,
    *,
    external_id: str,
    status: str = "CLOSED",
    rating: int | None = None,
    customer_name: str = "João Silva",
    customer_document: str = "99988877766",
    atendente_nome: str = "Atendente Maria",
) -> Atendimento:
    set_current_organization(org)
    return Atendimento.objects.create(
        organization=org, source_type="OPA", external_id=external_id,
        customer_name=customer_name, customer_document=customer_document,
        atendente_external_id="ag-1", atendente_nome=atendente_nome,
        status=status, canal="whatsapp", protocol=f"OPA-{external_id}",
        rating=rating, opened_at=timezone.now(),
    )


def _mensagem(
    org: Organization, at: Atendimento, *, ext: str, direction: str, texto: str
) -> Mensagem:
    set_current_organization(org)
    return Mensagem.objects.create(
        organization=org, source_type="OPA", external_id=ext, atendimento=at,
        atendimento_external_id=at.external_id, direction=direction,
        texto=texto, sent_at=timezone.now(),
    )


class _FakeJudge:
    """Juiz LLM fake — devolve um payload fixo e registra a chamada."""

    def __init__(self, payload: dict[str, Any]) -> None:
        self.payload = json.dumps(payload)
        self.calls: list[dict[str, Any]] = []

    def judge(self, *, system: str, user: str, model: str) -> str:
        self.calls.append({"system": system, "user": user, "model": model})
        return self.payload


# =============================================================================
# review_conversation — com juiz injetado (sem rede)
# =============================================================================
@pytest.mark.django_db
class TestReviewConversation:
    def test_persists_parsed_review(self, organization_a: Organization) -> None:
        at = _atendimento(organization_a, external_id="qa1", rating=2)
        _mensagem(organization_a, at, ext="m1", direction="CLIENT",
                  texto="minha internet caiu, cpf 123.456.789-09")
        _mensagem(organization_a, at, ext="m2", direction="AGENT",
                  texto="vou verificar agora")
        judge = _FakeJudge({
            "resolveu": True, "sla_ok": True, "tom": 4, "empatia": 5,
            "aderencia": 3, "categoria": "sem conexão",
            "resumo": "Cliente sem internet, resolvido.",
            "melhoria": "Confirmar a solução no fim.", "overall_score": 82,
        })
        review = review_conversation(organization_a, at, client=judge)
        assert review is not None
        assert review.overall_score == 82
        assert review.tom == 4
        assert review.empatia == 5
        assert review.resolveu is True
        assert review.categoria == "sem conexão"
        # snapshot do atendente preservado
        assert review.atendente_nome == "Atendente Maria"
        assert review.model_name  # default das settings

    def test_transcript_is_redacted_before_judge(
        self, organization_a: Organization
    ) -> None:
        at = _atendimento(organization_a, external_id="qa2")
        _mensagem(organization_a, at, ext="m1", direction="CLIENT",
                  texto="aqui é João Silva, cpf 123.456.789-09")
        judge = _FakeJudge({"overall_score": 50})
        review_conversation(organization_a, at, client=judge)
        sent = judge.calls[0]["user"]
        # PII não sai do cluster.
        assert "João" not in sent
        assert "123.456.789-09" not in sent
        assert "[CPF]" in sent

    def test_no_messages_returns_none(self, organization_a: Organization) -> None:
        at = _atendimento(organization_a, external_id="qa3")
        judge = _FakeJudge({"overall_score": 50})
        assert review_conversation(organization_a, at, client=judge) is None
        assert judge.calls == []  # nem chamou o juiz

    def test_malformed_judge_response_returns_none(
        self, organization_a: Organization
    ) -> None:
        at = _atendimento(organization_a, external_id="qa4")
        _mensagem(organization_a, at, ext="m1", direction="CLIENT", texto="oi")

        class _BadJudge:
            def judge(self, *, system: str, user: str, model: str) -> str:
                return "desculpe, não consegui"

        assert review_conversation(organization_a, at, client=_BadJudge()) is None
        set_current_organization(organization_a)
        assert not QAReview.objects.filter(atendimento=at).exists()

    def test_re_review_upserts(self, organization_a: Organization) -> None:
        at = _atendimento(organization_a, external_id="qa5")
        _mensagem(organization_a, at, ext="m1", direction="CLIENT", texto="oi")
        review_conversation(organization_a, at, client=_FakeJudge({"overall_score": 30}))
        review_conversation(organization_a, at, client=_FakeJudge({"overall_score": 70}))
        set_current_organization(organization_a)
        rows = QAReview.objects.filter(atendimento=at)
        assert rows.count() == 1
        assert rows.first().overall_score == 70

    def test_clamps_out_of_range_values(
        self, organization_a: Organization
    ) -> None:
        at = _atendimento(organization_a, external_id="qa6")
        _mensagem(organization_a, at, ext="m1", direction="CLIENT", texto="oi")
        judge = _FakeJudge({"tom": 9, "overall_score": 250, "empatia": "x"})
        review = review_conversation(organization_a, at, client=judge)
        assert review.tom == 5  # capado em 5
        assert review.overall_score == 100  # capado em 100
        assert review.empatia is None  # não-numérico → None


# =============================================================================
# Seletor de amostragem
# =============================================================================
@pytest.mark.django_db
class TestSelectForQA:
    def test_excludes_reviewed_and_prioritizes_low_rating(
        self, organization_a: Organization
    ) -> None:
        ruim = _atendimento(organization_a, external_id="s1", rating=1)
        bom = _atendimento(organization_a, external_id="s2", rating=5)
        _atendimento(organization_a, external_id="s3", status="OPEN")  # aberto: fora
        ja_revisado = _atendimento(organization_a, external_id="s4", rating=2)
        set_current_organization(organization_a)
        QAReview.objects.create(
            organization=organization_a, atendimento=ja_revisado,
            overall_score=50, reviewed_at=timezone.now(),
        )

        selected = select_conversations_for_qa(organization_a, limit=10)
        ids = [a.id for a in selected]
        assert ja_revisado.id not in ids  # já avaliado é excluído
        assert all(a.status == "CLOSED" for a in selected)  # aberto fora
        # nota baixa primeiro
        assert selected[0].id == ruim.id
        assert bom.id in ids

    def test_respects_limit(self, organization_a: Organization) -> None:
        for i in range(5):
            _atendimento(organization_a, external_id=f"lim-{i}", rating=1)
        assert len(select_conversations_for_qa(organization_a, limit=3)) == 3


# =============================================================================
# Beat task de amostragem — guard de QA_LLM_ENABLED (sem rede)
# =============================================================================
@pytest.mark.django_db
class TestQaBeatTasks:
    def test_dispatch_is_noop_when_disabled(
        self, organization_a: Organization, settings: Any
    ) -> None:
        settings.QA_LLM_ENABLED = False
        from apps.analytics.tasks import _dispatch_qa_reviews

        assert _dispatch_qa_reviews() == {"dispatched": 0}

    def test_run_skips_when_disabled(
        self, organization_a: Organization, settings: Any
    ) -> None:
        settings.QA_LLM_ENABLED = False
        from apps.analytics.tasks import _run_qa_reviews

        out = _run_qa_reviews(organization_id=organization_a.id, limit=5)
        assert out["skipped"] == "disabled"

    def test_run_reviews_candidates_when_enabled(
        self,
        organization_a: Organization,
        settings: Any,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        settings.QA_LLM_ENABLED = True
        at = _atendimento(organization_a, external_id="bt1", rating=1)
        _mensagem(organization_a, at, ext="m1", direction="CLIENT", texto="oi")

        calls: list[int] = []

        def _fake_review(org: Organization, atendimento: Atendimento) -> object:
            calls.append(atendimento.id)
            return object()

        monkeypatch.setattr(
            "apps.analytics.application.qa_review.review_conversation",
            _fake_review,
        )
        from apps.analytics.tasks import _run_qa_reviews

        out = _run_qa_reviews(organization_id=organization_a.id, limit=5)
        assert out == {"reviewed": 1, "candidates": 1}
        assert calls == [at.id]
