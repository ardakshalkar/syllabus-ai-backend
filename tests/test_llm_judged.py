"""Every syllabus criterion is judged by the model; code only states facts, as hints the model checks."""

from pathlib import Path

import yaml

from app.ai.checks.criteria import STRUCTURE_HINTS, build_user_prompt
from app.ai.checks.technical import build_report
from app.ai.types import AssessmentItem, CriterionSpec, ExtractedContent, Outcome
from app.enums import EvaluationType

RUBRIC = Path(__file__).resolve().parents[1] / "rubric" / "2026.3.yaml"


def _content(assessments: list[AssessmentItem]) -> ExtractedContent:
    return ExtractedContent(
        raw_text="x",
        paragraphs=[],
        outcomes=[Outcome(code="LO1", text="Analyse a market."), Outcome(code="LO2", text="Apply a model.")],
        assessments=assessments,
    )


def test_rubric_2026_3_has_no_code_rules():
    rubric = yaml.safe_load(RUBRIC.read_text(encoding="utf-8"))
    criteria = [c for p in rubric["principles"] for c in p["criteria"]]
    assert rubric["code"] == "2026.3"
    assert len(criteria) == 49
    assert not [c for c in criteria if c.get("evaluation_type") == "deterministic"]
    outcomes_block = rubric["principles"][2]["criteria"][2]
    assert outcomes_block["name"] == "Learning outcomes are a separate, structured block"
    assert outcomes_block["critical"] is True


def test_outcomes_unassessed_only_when_the_assessment_names_outcomes():
    # The assessment table names no outcomes at all: the mapping is elsewhere, nothing is "unassessed".
    report = build_report(_content([AssessmentItem(text="Exam", weight=100)]))
    assert report.unassessed_outcomes == []
    # It names LO1 only: LO2 is left out.
    report = build_report(_content([AssessmentItem(text="Exam", weight=100, outcomes=["LO1"])]))
    assert report.unassessed_outcomes == ["LO2"]


def test_structure_findings_reach_the_model_as_hints():
    content = _content([AssessmentItem(text="Exam", weight=60, outcomes=["LO1"])])
    report = build_report(content)
    spec = CriterionSpec(
        code="6.1",
        name="Every outcome is assessed",
        principle=6,
        principle_name="Assessment",
        evaluation_type=EvaluationType.ai_semantic,
    )
    prompt = build_user_prompt([spec], content, report, {})
    assert STRUCTURE_HINTS in prompt
    assert "reliable" not in prompt
    assert "LO2 is not mapped to an assessment" in prompt
