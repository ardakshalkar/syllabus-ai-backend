import uuid
from datetime import datetime

from pydantic import Field

from app.ai.types import CriterionResultData, TechnicalReport
from app.enums import AiStatus, ApprovalStage, CheckKind, RunStatus, Severity, Trigger, VersionSource
from app.schemas.common import Schema


class SyllabusOut(Schema):
    id: uuid.UUID
    course_id: uuid.UUID
    course_name: str
    course_slug: str
    course_lead_id: uuid.UUID | None = Field(description="None for a course read from Google Drive")
    program_id: uuid.UUID
    program_name: str
    school_id: uuid.UUID
    school_name: str
    professor_id: uuid.UUID | None
    professor_name: str | None
    language: str
    semester: str
    current_version: int
    ai_status: AiStatus
    approval_stage: ApprovalStage
    issues: int | None
    critical_issues: int | None = Field(default=None, description="of its issues, how many are critical")
    major_issues: int | None = Field(default=None, description="of its issues, how many are major")
    folder_path: str | None
    updated_at: datetime
    discipline: str | None = Field(
        default=None, description="its Drive folder, for a syllabus read from Drive"
    )
    drive_link: str | None = Field(default=None, description="the current version's file in Google Drive")
    in_drive: bool = Field(default=False, description="the current version is read from Google Drive")
    drive_missing: bool = Field(default=False, description="the file is no longer in its Drive folder")
    last_checked_version: int | None = None
    drive_changed: bool = Field(
        default=False, description="the file changed in Drive after its last check: check it again"
    )


class TransitionOption(Schema):
    to_stage: ApprovalStage
    label: str
    comment_required: bool


class SyllabusDetail(SyllabusOut):
    is_course_lead: bool
    can_check: bool = Field(
        default=False, description="may start an AI check: the lead or the School's managers"
    )
    available_transitions: list[TransitionOption]
    latest_run_id: uuid.UUID | None


class SyllabusCreate(Schema):
    course_id: uuid.UUID
    language: str = "English"
    semester: str
    folder_id: uuid.UUID | None = None


class VersionOut(Schema):
    id: uuid.UUID
    syllabus_id: uuid.UUID
    version: int
    filename: str
    content_type: str
    size: int
    created_at: datetime
    created_by: uuid.UUID | None
    created_by_name: str | None = None
    issue_count: int | None
    source: VersionSource
    drive_synced_at: datetime | None
    drive_modified_at: datetime | None = Field(default=None, description="when the file changed in Drive")


class RunProgress(Schema):
    """Where a Queued or Processing check is, for the progress window. Times are estimates."""

    phase: str = Field(description='e.g. "Evaluating 45 criteria (3/9 principles)", or "Queued"')
    step: int = Field(description="index into `steps` of the step being done")
    steps: list[str]
    percent: int = Field(ge=0, le=99)
    principles_done: int
    principles_total: int
    running: list[str] = Field(description='principles being evaluated right now, e.g. "4. Assessment"')
    started_at: datetime | None
    elapsed_s: int
    eta_s: int = Field(description="estimated seconds until the check finishes")
    eta_at: datetime = Field(description="estimated time the check finishes")
    queue_position: int | None = Field(
        default=None, description="Queued only: checks waiting ahead of this one"
    )


class RunOut(Schema):
    id: uuid.UUID
    version_id: uuid.UUID
    rubric_version: str | None = None
    model_version: str | None
    checker_version: str | None
    trigger: Trigger
    session_id: uuid.UUID | None
    checks: list[CheckKind]
    status: RunStatus
    # While Processing: what the worker is doing, e.g. "Evaluating 45 criteria (3/9 principles)".
    phase: str | None = None
    progress: RunProgress | None = None
    created_at: datetime
    started_at: datetime | None
    completed_at: datetime | None
    error: str | None


class CriterionResultOut(CriterionResultData):
    name: str
    principle: int
    critical: bool = Field(default=False, description="the rubric marks this criterion critical")
    severity: Severity | None = Field(default=None, description="None when the result is no finding")


class RunResultsOut(RunOut):
    issues: int | None = None
    critical_issues: int | None = None
    major_issues: int | None = None
    minor_issues: int | None = None
    technical: TechnicalReport | None = None
    results: list[CriterionResultOut] = Field(default_factory=list)


class UploadOut(Schema):
    version: VersionOut
    run: RunOut


class TransitionIn(Schema):
    to_stage: ApprovalStage
    comment: str | None = None


class CommentIn(Schema):
    text: str = Field(min_length=1, max_length=5000)


class ActivityOut(Schema):
    id: uuid.UUID
    kind: str
    from_stage: ApprovalStage | None
    to_stage: ApprovalStage | None
    version: int | None
    actor_id: uuid.UUID | None
    actor_name: str
    comment: str | None
    created_at: datetime


class StatusChange(Schema):
    criterion_id: str
    before: str | None
    after: str | None


class CompareOut(Schema):
    a: VersionOut
    b: VersionOut
    issues_before: int | None
    issues_after: int | None
    criterion_changes: list[StatusChange]
    text_diff: list[str]
