import uuid
from datetime import datetime
from typing import Literal

from pydantic import Field

from app.enums import CheckKind, SessionItemStatus, SessionScope, SessionStatus, Trigger
from app.schemas.common import Schema
from app.schemas.syllabus import RunOut, RunProgress, VersionOut


class SessionCreate(Schema):
    name: str = Field(min_length=1, max_length=200)
    scope_type: SessionScope
    scope_ref: uuid.UUID | None = None
    semester: str
    checks: list[CheckKind] = Field(default_factory=lambda: list(CheckKind))
    scheduled_at: datetime | None = None
    notify_provost: bool = True
    notify_school: bool = False
    only_unchecked: bool = Field(
        default=False, description="check only syllabi the AI has not checked yet, skipping the rest"
    )


class SessionProgress(Schema):
    total: int
    done: int
    failed: int
    completed: int
    pending: int


class SessionOut(Schema):
    id: uuid.UUID
    name: str
    scope_type: SessionScope
    scope_ref: uuid.UUID | None
    semester: str
    checks: list[CheckKind]
    status: SessionStatus
    scheduled_at: datetime | None
    started_at: datetime | None
    completed_at: datetime | None
    created_at: datetime
    created_by: uuid.UUID | None
    created_by_name: str | None
    notify_provost: bool
    notify_school: bool
    only_unchecked: bool
    progress: SessionProgress


class SessionItemOut(Schema):
    id: uuid.UUID
    syllabus_id: uuid.UUID
    version_id: uuid.UUID
    run_id: uuid.UUID | None
    course_name: str
    filename: str
    version: int
    status: SessionItemStatus
    # While Processing: what the worker is doing, e.g. "Evaluating 45 criteria (3/9 principles)".
    phase: str | None = None
    progress: RunProgress | None = None
    issues: int | None
    error: str | None
    updated_at: datetime


class ProposalItemOut(Schema):
    id: str
    criterion_id: str
    title: str
    current: str
    proposed: str
    reason: str
    anchor_found: bool
    decision: Literal["pending", "accepted", "rejected"]
    edited: bool


class ProposalsOut(Schema):
    id: str
    run_id: uuid.UUID
    version_id: uuid.UUID
    syllabus_id: uuid.UUID
    applied_version_id: uuid.UUID | None
    items: list[ProposalItemOut]
    created_at: datetime


class ProposalDecision(Schema):
    decision: Literal["pending", "accepted", "rejected"]
    proposed: str | None = None


class ApplyOut(Schema):
    version: VersionOut
    run: RunOut
    appended: int = Field(
        description="Accepted edits whose text was not found and were added under 'Revisions'"
    )


class PrecheckStarted(Schema):
    job_id: str


class PrecheckOut(Schema):
    job_id: str
    course_id: str
    course_name: str
    filename: str
    status: Literal["Queued", "Processing", "Completed", "Failed"]
    phase: str | None
    progress: RunProgress | None = None
    result: dict | None
    error: str | None


class DraftIn(Schema):
    course_id: uuid.UUID | None = None
    title: str = ""
    language: str = "English"
    semester: str = "Fall 2026"
    degree_level: str = "Bachelor"
    fields: dict[str, str] = Field(default_factory=dict)
    step: int = 0


class DraftPatch(Schema):
    course_id: uuid.UUID | None = None
    title: str | None = None
    language: str | None = None
    semester: str | None = None
    degree_level: str | None = None
    fields: dict[str, str] | None = None
    step: int | None = None


class DraftOut(Schema):
    id: str
    course_id: uuid.UUID | None
    title: str
    language: str
    semester: str
    degree_level: str
    fields: dict[str, str]
    step: int
    syllabus_id: uuid.UUID | None
    updated_at: datetime


class AssistantIn(Schema):
    section: str
    question: str = Field(min_length=1, max_length=2000)


class AssistantOut(Schema):
    section: str
    answer: str


class GenerateOut(Schema):
    syllabus_id: uuid.UUID
    version: VersionOut
    run: RunOut


class OverviewSchool(Schema):
    school_id: uuid.UUID
    school_name: str
    expected: int
    found: int
    checked: int
    needs_revision: int
    awaiting_approval: int
    approved: int
    last_activity: datetime | None


class HeatCell(Schema):
    principle: int
    flagged: int
    percent: int


class HeatRow(Schema):
    school_id: uuid.UUID
    school_name: str
    syllabi_checked: int
    cells: list[HeatCell]


class PrincipleRef(Schema):
    number: int
    name: str


class HeatmapOut(Schema):
    semester: str
    principles: list[PrincipleRef]
    rows: list[HeatRow]


class FrequentIssue(Schema):
    criterion_id: str
    name: str
    syllabi: int
    percent: int


class TriggerCount(Schema):
    trigger: Trigger
    runs: int


class NotificationOut(Schema):
    id: uuid.UUID
    kind: str
    title: str
    body: str | None
    payload: dict
    read_at: datetime | None
    created_at: datetime
