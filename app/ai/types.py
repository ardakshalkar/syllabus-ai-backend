"""Storage-agnostic data passed through the AI pipeline.

Kept free of database types so the private pre-check can run the same pipeline without persisting anything.
"""

from pydantic import BaseModel, Field

from app.enums import EvaluationType, ResultStatus


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


class TechnicalIssue(BaseModel):
    code: str
    message: str


class TechnicalReport(BaseModel):
    issues: list[TechnicalIssue] = Field(default_factory=list)
    missing_sections: list[str] = Field(default_factory=list)
    weights_total: float | None = None
    unassessed_outcomes: list[str] = Field(default_factory=list)


class CriterionResultData(BaseModel):
    criterion_id: str
    status: ResultStatus
    evidence: str
    explanation: str
    recommendation: str
    required_context: str | None = None
    confidence: float | None = None
    evidence_verified: bool | None = None


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
