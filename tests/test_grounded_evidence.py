"""Failed citations are re-evaluated against source passage IDs without weakening quote validation."""

import pytest

from app.ai import pipeline
from app.ai.decisions import Decision
from app.ai.extraction import parse_lines
from app.ai.fake import FakeProvider
from app.ai.llm import LLMError
from app.ai.types import CriterionSpec
from app.enums import EvaluationType, ResultStatus

SPEC = CriterionSpec(
    code="6.6",
    name="Multiple assessment types are used",
    principle=6,
    principle_name="Assessment",
    evaluation_type=EvaluationType.ai_semantic,
    guidance="Count distinct assessment instruments, not the number of assignments.",
)


class Decider:
    model_version = "decision"

    async def decide(self, state, specs, **kwargs):
        return {"6.6": Decision(ResultStatus.not_compliant, 0.9)}


class Provider(FakeProvider):
    def __init__(self, mode="valid", status="Not compliant"):
        self.mode, self.status, self.repairs = mode, status, 0

    async def complete_json(self, **kwargs):
        if kwargs["purpose"] == "explain":
            return {
                "results": [
                    {
                        "criterion_id": "6.6",
                        "evidence": "Form of assessment | Written work"
                        if self.mode == "valid" and self.status == "Not compliant"
                        else "An invented passage about an assessment",
                        "explanation": "All tasks use written work.",
                        "recommendation": "Use different instruments.",
                    }
                ]
            }
        assert kwargs["purpose"] == "evidence_repair"
        self.repairs += 1
        if self.mode == "error":
            raise LLMError("repair unavailable")
        source = kwargs["meta"]["passages"]
        chosen = [i for i, text in enumerate(source, 1) if text.startswith("Task ")]
        if self.mode == "invalid":
            chosen = [len(source) + 1]
        return {
            "results": [
                {
                    "criterion_id": "6.6",
                    "status": self.status,
                    "passage_ids": chosen,
                    "explanation": "The actual assessment rows establish the available instruments.",
                    "recommendation": "Use different instruments.",
                    "confidence": 0.9,
                }
            ]
        }


def content(second="Written work"):
    return parse_lines(
        [
            ("Assessment", True),
            ("Form of assessment | Points", False),
            ("Task 1 | Written work | 50", False),
            (f"Task 2 | {second} | 50", False),
            ("Final exam | Written work | 100", False),
        ]
    )


async def test_single_mode_finding_survives_bad_table_citation():
    llm = Provider()
    output = await pipeline.evaluate(content(), [SPEC], llm, trace=False, decider=Decider())
    result = output.results[0]
    assert llm.repairs == 0
    assert result.status == ResultStatus.not_compliant
    assert result.evidence_verified is True
    assert "Form of assessment" in result.evidence and "Written work" in result.evidence
    assert "could not be found" not in result.explanation


async def test_repair_reconsiders_status_using_real_assessment_forms():
    llm = Provider(status="Compliant")
    output = await pipeline.evaluate(content("Project defence"), [SPEC], llm, trace=False, decider=Decider())
    result = output.results[0]
    assert result.status == ResultStatus.compliant
    assert "Project defence" in result.evidence
    assert result.evidence_verified is True
    assert result.recommendation == "No change suggested for this criterion."


@pytest.mark.parametrize("mode", ["invalid", "error"])
async def test_unavailable_or_fabricated_repair_cannot_override_evidence_guard(mode):
    output = await pipeline.evaluate(content(), [SPEC], Provider(mode), trace=False, decider=Decider())
    result = output.results[0]
    assert result.status == ResultStatus.cannot_verify
    assert result.evidence_verified is False


def test_table_recovery_does_not_accept_invented_cells():
    from app.ai.evidence import recover_table_quote
    from app.ai.types import CriterionResultData

    result = CriterionResultData(
        criterion_id="6.6",
        status=ResultStatus.not_compliant,
        evidence="Form of assessment | Oral presentation",
        explanation="Only one form.",
        recommendation="",
    )
    assert recover_table_quote(result, content().raw_text).evidence == result.evidence


def test_table_recovery_marks_separate_source_cells_as_separate_quotes():
    from app.ai.evidence import recover_table_quote, verify
    from app.ai.types import CriterionResultData

    result = CriterionResultData(
        criterion_id="6.6",
        status=ResultStatus.not_compliant,
        evidence="Form of assessment | Written work",
        explanation="Only one form.",
        recommendation="",
    )
    fixed = verify(recover_table_quote(result, content().raw_text), content().raw_text, 85)
    assert fixed.status == ResultStatus.not_compliant
    assert fixed.evidence_verified is True
    assert "..." in fixed.evidence
