"""Storage-agnostic data passed through the AI pipeline.

Kept free of database types so the private pre-check can run the same pipeline without persisting anything.
"""

from pydantic import BaseModel, Field

from app.enums import EvaluationType, ResultStatus

# The languages the app is read in. A text in all of them is a {code: text} dict.
LANGUAGES = ("en", "ru", "kk")
LANGUAGE_NAMES = {"en": "English", "ru": "Russian", "kk": "Kazakh"}
LocalizedText = dict[str, str]


def language_code(name: str) -> str:
    """The code of a language named in settings (LLM_OUTPUT_LANGUAGE=English), or the code itself."""
    lowered = name.strip().lower()
    for code, full in LANGUAGE_NAMES.items():
        if lowered in (code, full.lower()):
            return code
    return lowered


class CriterionTexts(BaseModel):
    """What a finding on a criterion means and how to fix it, written once by people in every language.
    Shown above the AI's explanation and recommendation for the particular syllabus."""

    problem: LocalizedText
    fix: LocalizedText


class ResultText(BaseModel):
    explanation: str = ""
    recommendation: str = ""


class Outcome(BaseModel):
    code: str
    text: str


class Week(BaseModel):
    number: int
    text: str


class AssessmentItem(BaseModel):
    text: str
    weight: float | None = None
    outcomes: list[str] = Field(default_factory=list)


class Block(BaseModel):
    """One line of `raw_text` (a paragraph or a table row) and the kind of content it holds (app.ai.blocks).
    Findings cite a block by its number: its position in `ExtractedContent.blocks`, counted from 1."""

    text: str
    category: str


class ExtractedContent(BaseModel):
    language: str | None = None
    raw_text: str
    paragraphs: list[str]
    headings: list[str] = Field(default_factory=list)
    sections: dict[str, str] = Field(default_factory=dict)
    outcomes: list[Outcome] = Field(default_factory=list)
    weeks: list[Week] = Field(default_factory=list)
    assessments: list[AssessmentItem] = Field(default_factory=list)
    resources: list[str] = Field(default_factory=list)
    # The text was read from page images by a vision model (app.ai.ocr): a scan with no text layer.
    read_by_ocr: bool = False
    # raw_text line by line with a category each; empty until app.ai.blocks.categorize has run.
    blocks: list[Block] = Field(default_factory=list)


class CriterionSpec(BaseModel):
    code: str
    name: str
    principle: int
    principle_name: str
    evaluation_type: EvaluationType
    required_context: str | None = None
    rule: str | None = None
    guidance: str | None = None
    critical: bool = False
    texts: CriterionTexts | None = None


class TechnicalIssue(BaseModel):
    code: str
    message: str


class TechnicalReport(BaseModel):
    issues: list[TechnicalIssue] = Field(default_factory=list)
    missing_sections: list[str] = Field(default_factory=list)
    weights_total: float | None = None
    unassessed_outcomes: list[str] = Field(default_factory=list)


class QuoteContext(BaseModel):
    """Where a quote stands in the syllabus, so it can be shown in place rather than as a bare fragment: the table
    row that holds it (its non-empty cells, the one with the quote, and the column names when the table has
    them), or the paragraph."""

    cells: list[str] = Field(default_factory=list)
    header: list[str] = Field(default_factory=list)  # same length as cells, or empty
    cell: int | None = None  # index in cells of the one holding the quote
    text: str = ""  # the paragraph, when the quote is not in a table


class CriterionResultData(BaseModel):
    criterion_id: str
    status: ResultStatus
    evidence: str
    explanation: str
    recommendation: str
    required_context: str | None = None
    confidence: float | None = None
    evidence_verified: bool | None = None
    # The block (ExtractedContent.blocks, counted from 1) the evidence was taken from, when the model cited one.
    evidence_block: int | None = None
    evidence_context: QuoteContext | None = None
    # The language explanation and recommendation are written in, and their translations into the other
    # languages of the app, keyed by language code. Results stored before translation have neither.
    language: str | None = None
    translations: dict[str, ResultText] = Field(default_factory=dict)


class EvaluationOutput(BaseModel):
    technical: TechnicalReport
    results: list[CriterionResultData]
    model_version: str
    checker_version: str


class ProposalDraft(BaseModel):
    criterion_id: str
    title: str
    current: str
    proposed: str
    reason: str
    anchor_found: bool = True
