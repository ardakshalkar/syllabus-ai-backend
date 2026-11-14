import uuid
from datetime import datetime
from typing import Literal

from pydantic import Field

from app.enums import ResultStatus, Severity
from app.schemas.common import Schema
from app.schemas.work import FrequentIssue


class SeverityCounts(Schema):
    """Syllabi of a scope by their worst finding, and those with no result."""

    found: int
    checked: int
    with_critical: int = Field(description="checked syllabi with at least one critical finding")
    major_only: int = Field(description="checked syllabi whose worst finding is major")
    minor_only: int = Field(description="checked syllabi whose findings are all minor")
    clean: int = Field(description="checked syllabi with no findings")
    cannot_check: int
    not_checked: int


class TopIssue(Schema):
    criterion_id: str
    name: str
    critical: bool


class AnalysisSchool(SeverityCounts):
    school_id: uuid.UUID
    school_name: str
    top_issue: TopIssue | None = Field(description="its critical criterion failed most, else any criterion")


class AttentionCourse(Schema):
    syllabus_id: uuid.UUID
    course_name: str
    professor_name: str | None
    language: str
    critical: int
    major: int
    minor: int
    top_issue: TopIssue | None


class AnalysisOut(Schema):
    semester: str
    school_id: uuid.UUID | None
    school_name: str | None
    totals: SeverityCounts
    schools: list[AnalysisSchool]
    issues: list[FrequentIssue] = Field(
        description="every criterion failed at least once, most frequent first"
    )
    attention: list[AttentionCourse] = Field(
        description="within one School: the courses needing attention most"
    )
    report_limit: int = Field(description="the most courses one report holds")


class ReportFinding(Schema):
    criterion_id: str
    name: str
    principle: int
    principle_name: str
    severity: Severity
    status: ResultStatus
    evidence: str
    explanation: str
    recommendation: str


class ReportCourse(Schema):
    syllabus_id: uuid.UUID
    course_name: str
    school_name: str
    program_name: str
    professor_name: str | None
    language: str
    critical: int
    major: int
    minor: int
    findings: list[ReportFinding]


class ReportOut(AnalysisOut):
    include: Literal["summary", "critical", "all"]
    generated_at: datetime
    attention: list[AttentionCourse] = Field(default_factory=list)
    courses: list[ReportCourse]
