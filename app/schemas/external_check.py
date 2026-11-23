import uuid

from pydantic import Field

from app.enums import ResultStatus
from app.schemas.common import Schema


class ExternalCriterionIn(Schema):
    criterion: str = Field(max_length=10, description="The rubric code, e.g. 1.1 (get_rubric lists them)")
    status: ResultStatus
    evidence: str = Field(
        default="",
        max_length=3000,
        description="A quote from the syllabus text that shows the status. It is checked against the text: "
        "a quote that is not in the document makes the criterion 'Cannot verify'.",
    )
    explanation: str = Field(default="", max_length=4000)
    recommendation: str = Field(
        default="", max_length=3000, description="What to change; empty when compliant"
    )
    confidence: float | None = Field(default=None, ge=0, le=1)


class ExternalCheckIn(Schema):
    model: str = Field(
        min_length=2, max_length=100, description="The AI that judged, e.g. 'Claude Opus' or 'ChatGPT 5'"
    )
    results: list[ExternalCriterionIn] = Field(
        min_length=1,
        max_length=200,
        description="One entry for EVERY criterion of the active rubric, none twice",
    )


class ExternalCheckOut(Schema):
    run_id: uuid.UUID
    version_id: uuid.UUID
    model_version: str
    issues: int
    critical: int
    major: int
    minor: int
    # Criteria whose quote was not found in the syllabus text, so their status became 'Cannot verify'.
    unverified: list[str]
