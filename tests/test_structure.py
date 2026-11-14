"""Markdown + LLM structure extraction: what the model returns is kept only if it is in the document."""

import asyncio
from typing import Any

from app.ai.checks.technical import build_report, evaluate_rule
from app.ai.evidence import evidence_found
from app.ai.structure import extract_structure
from app.ai.types import CriterionSpec
from app.enums import EvaluationType, ResultStatus

MARKDOWN = """**Course syllabus**

| Course title | **Theory of Probability** |
| --- | --- |
| Learning outcomes of the course | Upon completion of the course, students:   1. will use combinatorics formulas to find the probability of an event, 2. will be able to check the significance of the correlation coefficient. |
| Assessment methods and criteria | | Week | Assignment title | Topic | Weight | | --- | --- | --- | --- | |  | **ISC 1** |  | **30%** | | 4 | Task 1 | 1-4 |  | |  | **ISC 2** |  | **30%** | |  | **Final exam:** | **1-15** | **Project defense** | **40%** | |
| Attendance policy | At least 75% of classes. |
"""


class StubLLM:
    model_version = "stub"

    def __init__(self, reply: dict[str, Any]) -> None:
        self.reply = reply

    async def complete_json(self, **_: Any) -> dict[str, Any]:
        return self.reply

    async def complete_text(self, **_: Any) -> str:
        raise NotImplementedError


REPLY: dict[str, Any] = {
    "outcomes": [
        {"code": None, "text": "will use combinatorics formulas to find the probability of an event,"},
        {"code": None, "text": "will be able to check the significance of the correlation coefficient."},
        {"code": None, "text": "will design a rocket"},  # not in the document
    ],
    "outcomes_heading": "Learning outcomes of the course",
    "assessments": [
        {"name": "ISC 1", "weight_percent": 30, "parent": None, "outcomes": []},
        {"name": "Task 1", "weight_percent": 15, "parent": "ISC 1", "outcomes": []},
        {"name": "ISC 2", "weight_percent": 30, "parent": None, "outcomes": []},
        {"name": "Final exam: Project defense", "weight_percent": 40, "parent": None, "outcomes": []},
    ],
    "weeks": [],
    "sections": [
        {"kind": "outcomes", "heading": "Learning outcomes of the course"},
        {"kind": "assessment", "heading": "Assessment methods and criteria"},
        {"kind": "policy", "heading": "Attendance policy"},
    ],
}

SPEC = CriterionSpec(
    code="3.3",
    principle=3,
    principle_name="Learning objectives and outcomes",
    name="Learning outcomes are a separate, structured block",
    evaluation_type=EvaluationType.deterministic,
    rule="outcomes_numbered",
)


def test_unnumbered_outcomes_in_a_table_cell_count() -> None:
    content, report = asyncio.run(extract_structure(MARKDOWN, StubLLM(REPLY), trace=False))
    assert [o.code for o in content.outcomes] == ["LO1", "LO2"]
    assert report["dropped"] == ["outcome: will design a rocket"]
    result = evaluate_rule(SPEC, content, build_report(content))
    assert result.status == ResultStatus.compliant


def test_weights_count_top_level_components_only() -> None:
    content, _ = asyncio.run(extract_structure(MARKDOWN, StubLLM(REPLY), trace=False))
    # Task 1 sits inside ISC 1; a name that joins two cells ("Final exam: Project defense") is still found.
    assert build_report(content).weights_total == 100
    assert "Attendance policy" not in content.sections.get("outcomes", "")


def test_evidence_across_markdown_table_cells() -> None:
    assert evidence_found("ISC 1 30%", "|  | **ISC 1** | **30%** |", 85)


def test_evidence_shortened_with_ellipsis() -> None:
    doc = "1. Лекция-диалог на практических примерах. 2. Обучение с использованием реальных задач. 7. Кейс-метод"
    assert evidence_found(
        "Лекция-диалог на практических примерах... Обучение с использованием реальных задач", doc, 85
    )
    assert evidence_found("Лекция-диалог на практических примерах [and other methods]", doc, 85)
    # Every fragment must be there: one invented fragment rejects the quote.
    assert not evidence_found(
        "Лекция-диалог на практических примерах... Защита диссертации в Оксфорде", doc, 85
    )
