"""Numbered, categorized lines of a syllabus (app.ai.blocks) and quotes taken from them."""

from app.ai import blocks
from app.ai.checks.criteria import build_user_prompt, explain_schema, quote_from_block
from app.ai.checks.technical import build_report
from app.ai.evidence import evidence_found
from app.ai.types import CriterionSpec, ExtractedContent
from app.config import get_settings
from app.enums import EvaluationType, ResultStatus

MARKDOWN = """**Course syllabus**

| Course title | **Applied Statistics** |
| --- | --- |
| Посещаемость: политика посещаемости учебных занятий | Не менее 75% занятий |
| Learning outcomes of the course | 1. Design a controlled experiment and compute its sample size. |
| 3 | Week three: hypothesis tests |
The course teaches students to reason about data.
Students work in teams on a project."""


class _Sorter:
    model_version = "fake"

    def __init__(self, answer):
        self.answer, self.asked = answer, None

    async def complete_json(self, *, purpose, system, user, schema, meta=None, run_id=None, trace=True):
        self.asked = schema["properties"]["lines"]["items"]["properties"]["n"]["enum"]
        return {"lines": [{"n": n, "category": c} for n, c in self.answer.items()]}


def test_lines_drop_table_rules_and_labels_give_categories():
    lines = blocks.lines(MARKDOWN)
    assert len(lines) == 7 and not any(set(line) <= set("|- ") for line in lines)
    assert [blocks.by_label(line) for line in lines] == [
        None,
        "course_info",
        "policy",
        "outcomes",
        "content",
        None,
        None,
    ]
    assert blocks.text_of(lines[1]) == "Course title | Applied Statistics"


def test_long_rows_are_split_into_passages_that_keep_the_row_label():
    cells = "  ".join(
        f"{n}. Students will be able to do skill number {n} at a measurable level." for n in range(1, 13)
    )
    markdown = f"| Learning outcomes of the course | {cells} |\n| --- | --- |\nShort line."
    parts = blocks.pieces(markdown)
    assert len(parts) > 2 and all(len(text) <= blocks.MAX_BLOCK for text, _ in parts)
    assert {category for _, category in parts[:-1]} == {"outcomes"} and parts[-1] == ("Short line.", None)
    assert all(evidence_found(text, markdown, 85) for text, _ in parts)


async def test_the_model_sorts_only_unlabelled_lines():
    sorter = _Sorter({1: "course_info", 6: "goals"})  # leaves line 7 out
    result = await blocks.categorize(MARKDOWN, sorter)
    assert sorter.asked == [1, 6, 7]
    assert [b.category for b in result] == [
        "course_info",
        "course_info",
        "policy",
        "outcomes",
        "content",
        "goals",
        "goals",  # left out: the category of the line before
    ]
    # A quote taken from a block is found in the document the checks read.
    assert all(evidence_found(b.text[:200], MARKDOWN, 85) for b in result if len(b.text) >= 8)


async def test_explanations_cite_numbered_lines(monkeypatch):
    content = ExtractedContent(raw_text=MARKDOWN, paragraphs=blocks.lines(MARKDOWN))
    content.blocks = await blocks.categorize(MARKDOWN, _Sorter({1: "other", 6: "goals", 7: "methods"}))
    monkeypatch.setattr(get_settings(), "explain_blocks", True)
    spec = CriterionSpec(
        code="9.3",
        name="Attendance policy is stated",
        principle=9,
        principle_name="Course policy",
        evaluation_type=EvaluationType.ai_semantic,
    )
    prompt = build_user_prompt([spec], content, build_report(content), {}, {"9.3": ResultStatus.compliant})
    assert "[3·policy] Посещаемость" in prompt
    assert "quote" in explain_schema(["9.3"], numbered=True)["properties"]["results"]["items"]["required"]

    # The copied words when they are in the cited line; the line itself when they are not.
    assert quote_from_block({"line": 3, "quote": "Не менее 75% занятий"}, content) == (
        "Не менее 75% занятий",
        3,
    )
    evidence, block = quote_from_block({"line": 7, "quote": "invented words here"}, content)
    assert (evidence, block) == ("Students work in teams on a project.", 7)
    # A line that does not exist keeps the copied words, which the evidence check then judges.
    assert quote_from_block({"line": 99, "quote": "teams on a project"}, content) == (
        "teams on a project",
        None,
    )


def test_a_line_number_written_as_the_quote_takes_the_line():
    content = ExtractedContent(raw_text=MARKDOWN, paragraphs=[])
    content.blocks = [blocks.Block(text=t, category=c or "other") for t, c in blocks.pieces(MARKDOWN)]
    assert quote_from_block({"line": 7, "quote": "7"}, content) == ("Students work in teams on a project.", 7)
    assert quote_from_block({"line": 7, "quote": "[7·methods] Students work in teams"}, content) == (
        "Students work in teams",
        7,
    )
