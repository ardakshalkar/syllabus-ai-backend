"""A check with one request to the paid model (Nov 2026): lines numbered in code, quotes taken from the cited
line, broken PDF text layers sent to OCR, and an estimate that prices reading and translation."""

import pytest

from app.ai import blocks
from app.ai.checks.criteria import evaluate_principle, response_schema
from app.ai.checks.technical import build_report
from app.ai.markdown import looks_broken
from app.ai.types import CriterionSpec, ExtractedContent
from app.config import get_settings
from app.enums import EvaluationType, ResultStatus
from app.services import check_mode, cost_estimate
from app.services.check_mode import CheckMode
from tests.test_blocks import MARKDOWN

SPEC = CriterionSpec(
    code="9.3",
    name="Attendance policy is stated",
    principle=9,
    principle_name="Course policy",
    evaluation_type=EvaluationType.ai_semantic,
)


class _NoModel:
    model_version = "none"

    async def complete_json(self, **_):
        raise AssertionError("no model request expected")


async def test_lines_are_numbered_without_a_model(monkeypatch):
    monkeypatch.setattr(get_settings(), "explain_blocks", True)
    monkeypatch.setattr(get_settings(), "llm_provider", "openrouter")
    content = ExtractedContent(raw_text=MARKDOWN, paragraphs=blocks.lines(MARKDOWN))
    assert await blocks.ensure(content, _NoModel())
    # The label's category, else the one of the line before.
    assert [b.category for b in content.blocks] == [
        "course_info",
        "course_info",
        "policy",
        "outcomes",
        "content",
        "content",
        "content",
    ]


class _Checker:
    model_version = "fake"

    def __init__(self, answer):
        self.answer, self.request = answer, None

    async def complete_json(self, *, purpose, system, user, schema, meta=None, run_id=None, trace=True):
        self.request = {"system": system, "user": user, "schema": schema}
        return {"results": [self.answer]}


async def test_the_check_cites_a_numbered_line_and_takes_the_quote_from_it(monkeypatch):
    monkeypatch.setattr(get_settings(), "explain_blocks", True)
    content = ExtractedContent(raw_text=MARKDOWN, paragraphs=blocks.lines(MARKDOWN))
    content.blocks = blocks.number(MARKDOWN)
    answer = {
        "criterion_id": "9.3",
        "status": "Compliant",
        "quote": "Не менее 75 % занятий",  # a space the line does not have
        "line": 3,
        "explanation": "",
        "recommendation": "",
        "confidence": 0.9,
    }
    checker = _Checker(answer)
    [result] = await evaluate_principle(checker, [SPEC], content, build_report(content), {})
    assert "[3·policy] Посещаемость" in checker.request["user"]
    assert '"line"' in checker.request["system"] and "{evidence_rule}" not in checker.request["system"]
    assert "quote" in checker.request["schema"]["properties"]["results"]["items"]["required"]
    assert result.status == ResultStatus.compliant and result.evidence_block == 3
    assert "75" in result.evidence


async def test_without_numbered_lines_the_check_copies_its_quote(monkeypatch):
    monkeypatch.setattr(get_settings(), "explain_blocks", False)
    content = ExtractedContent(raw_text=MARKDOWN, paragraphs=blocks.lines(MARKDOWN))
    answer = {
        "criterion_id": "9.3",
        "status": "Compliant",
        "evidence": "Не менее 75% занятий",
        "explanation": "",
        "recommendation": "",
        "confidence": 0.9,
    }
    checker = _Checker(answer)
    [result] = await evaluate_principle(checker, [SPEC], content, build_report(content), {})
    assert "evidence" in response_schema(["9.3"])["properties"]["results"]["items"]["required"]
    assert (result.evidence, result.evidence_block) == ("Не менее 75% занятий", None)


def test_a_scanner_text_layer_is_recognised_as_broken():
    row = "|     | N A R \\O | Z  |     |     |     |     |     |     |\n"
    broken = "| --- | --- |\n" + row * 40
    assert looks_broken(broken)
    word = "| Course title | Applied Statistics | 5 | 45 |\n| Prerequisites | - | | |\n"
    assert not looks_broken("| --- | --- |\n" + word * 40)
    assert not looks_broken(row * 5)  # too little to judge


PRICES = {"qwen/qwen4-max": (1.4e-6, 4.2e-6), "google/gemma-4-31b-it": (0.1e-6, 0.2e-6)}


def test_listed_single_check_includes_the_translation(monkeypatch):
    s = get_settings()
    monkeypatch.setattr(s, "translate_results", True)
    monkeypatch.setattr(s, "translation_model", "")
    mode = CheckMode(check_mode.SINGLE, "qwen/qwen4-max")
    alone = 15_000 * 1.4e-6 + 4_800 * 4.2e-6
    translated = 2_600 * 1.4e-6 + 5_100 * 4.2e-6
    assert cost_estimate._listed_check_usd(mode, PRICES) == pytest.approx(alone + translated)
    monkeypatch.setattr(s, "translation_model", "google/gemma-4-31b-it")
    cheap = 2_600 * 0.1e-6 + 5_100 * 0.2e-6
    assert cost_estimate._listed_check_usd(mode, PRICES) == pytest.approx(alone + cheap)


def test_reading_is_priced_at_the_models_that_will_read(monkeypatch):
    s = get_settings()
    monkeypatch.setattr(s, "llm_model", "qwen/qwen4-max")
    monkeypatch.setattr(s, "structure_model", "")
    monkeypatch.setattr(s, "explain_blocks", True)
    monkeypatch.setattr(s, "blocks_sort_by_model", False)
    tokens = {"structure": (8_000, 1_300), "blocks": (9_000, 2_300)}
    qwen = 8_000 * 1.4e-6 + 1_300 * 4.2e-6
    assert cost_estimate._reading_usd(tokens, PRICES) == pytest.approx(qwen)
    monkeypatch.setattr(s, "blocks_sort_by_model", True)
    assert cost_estimate._reading_usd(tokens, PRICES) == pytest.approx(qwen + 9_000 * 1.4e-6 + 2_300 * 4.2e-6)
    monkeypatch.setattr(s, "blocks_sort_by_model", False)
    monkeypatch.setattr(s, "structure_model", "google/gemma-4-31b-it")
    assert cost_estimate._reading_usd({}, PRICES) == pytest.approx(8_000 * 0.1e-6 + 1_300 * 0.2e-6)


DOCUMENT = """| Course title | Applied Statistics |
| --- | --- |
| Prerequisites | - |
| Learning outcomes | 1. Design a controlled experiment.<br>2. Apply negotiation and mediation instruments. |

| Weeks | Form of control | Share, % | Learning outcome |
| --- | --- | --- | --- |
| 1-7 | Class participation | 25 | LO 1, LO 4 |

| 1 | Theories of conflict | | 3 |
| --- | --- | --- | --- |
| 2 | Mediation practice | | 3 |

Students must **respect** each other and keep deadlines."""


def test_a_quote_comes_with_its_row_and_column_names():
    from app.ai.quote_context import context_of

    # A two-column row of the template: the label is the first cell, no column names.
    q = context_of("Apply negotiation and mediation instruments", DOCUMENT)
    assert q.header == [] and q.cells[0] == "Learning outcomes" and q.cell == 1
    assert "2. Apply negotiation" in q.cells[1] and "\n" in q.cells[1]
    # A table with column names.
    q = context_of("Class participation", DOCUMENT)
    assert q.header == ["Weeks", "Form of control", "Share, %", "Learning outcome"]
    assert q.cells == ["1-7", "Class participation", "25", "LO 1, LO 4"] and q.cell == 1
    # A table whose first row is data (week 1) has no column names; its empty cell is dropped.
    q = context_of("Mediation practice", DOCUMENT)
    assert q.header == [] and q.cells == ["2", "Mediation practice", "3"] and q.cell == 1
    # Short values are shown with the row they belong to.
    assert context_of("Prerequisites | -", DOCUMENT).cells == ["Prerequisites", "-"]
    # A paragraph, without its Markdown emphasis.
    q = context_of("must respect each other", DOCUMENT)
    assert q.text == "Students must respect each other and keep deadlines." and q.cells == []
    assert context_of("words that are nowhere in it", DOCUMENT) is None
