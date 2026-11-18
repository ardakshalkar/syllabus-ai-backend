import uuid
from datetime import datetime
from typing import Literal

from pydantic import Field, model_validator

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
    only_checked: bool = Field(
        default=False, description="check again only syllabi the AI has already checked, skipping the rest"
    )
    estimated_cost_usd: float | None = Field(
        default=None, ge=0, description="the estimate (POST /sessions/estimate) the user saw and agreed to"
    )

    @model_validator(mode="after")
    def _one_filter(self) -> "SessionCreate":
        if self.only_unchecked and self.only_checked:
            raise ValueError("only_unchecked and only_checked cannot both be set")
        return self


class SessionProgress(Schema):
    total: int
    done: int
    failed: int
    # Files the session never checked because it was cancelled; neither completed nor pending.
    cancelled: int = 0
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
    only_checked: bool
    estimated_cost_usd: float | None = None
    check_mode: str | None = Field(
        None, description="standard | single (services.check_mode); None: standard"
    )
    check_model: str | None = Field(None, description="the model of a single-request check")
    progress: SessionProgress


class EstimateIn(Schema):
    """Which syllabi a check would take: the same scope and filters as SessionCreate."""

    scope_type: SessionScope
    scope_ref: uuid.UUID | None = None
    semester: str
    only_unchecked: bool = False
    only_checked: bool = False


class EstimateFile(Schema):
    filename: str
    course: str
    pages: int | None = None
    reason: str | None = None


class AiServerOut(Schema):
    """One server the AI of a check runs on (app/services/ai_servers.py)."""

    role: str = Field(
        description="chat: checking, reading, scans, translating | decisions: the decision model"
    )
    kind: str = Field(
        description="openrouter | paid_api | own_server | unknown (internet, not recognised) | test"
    )
    name: str = Field(description='e.g. "OpenRouter", "Google Gemini API", "OpenAI (ChatGPT)", "Ollama"')
    host: str = Field(description='host and port of its address, e.g. "192.168.13.79:11434"')
    address: str | None = Field(description="the IP address the host resolves to, when found")
    network: str = Field(description="this_machine | local_network | internet | unknown")
    billed: bool = Field(description="each use costs money")
    model: str | None


class EstimateOut(Schema):
    """What a check would read and roughly what it would cost, in words a School administrator can follow."""

    files: int = Field(description="syllabi the check would take")
    text_files: int = Field(description="Word files and PDFs with text, read directly")
    scans: int = Field(description="scanned PDFs the OCR model has to read first")
    scan_pages: int = Field(description="pages of those scans that would be read (up to OCR_MAX_PAGES each)")
    already_read: int = Field(description="files read by an earlier check: their text is reused at no cost")
    unreadable: int = Field(description="files the check cannot read: unsupported type, empty, or damaged")
    not_looked_at: int = Field(description="files not opened in time for this estimate; estimated by average")
    ocr_enabled: bool
    billed: bool = Field(
        description="false when the models run on the university's own server (no per-use cost)"
    )
    servers: list[AiServerOut] = Field(description="where the AI of this check runs")
    cost_usd: float = Field(description="expected cost")
    cost_min_usd: float
    cost_max_usd: float
    check_usd: float = Field(description="part of the expected cost for checking against the criteria")
    reading_usd: float = Field(description="part for reading the documents' structure")
    ocr_usd: float = Field(description="part for reading scans")
    minutes: int = Field(description="rough time to finish, in minutes")
    check_mode: str = Field(description="how the AI will check, set by the administrator: standard | single")
    check_model: str | None = Field(description="the model of a single-request check")
    check_mode_label: str = Field(description='e.g. "One request: qwen4-max"')
    check_per_syllabus_usd: float = Field(
        description="checking one syllabus against the criteria in this mode"
    )
    check_price_measured: bool = Field(
        description="true: measured from recent checks in this mode; false: from the model's list price"
    )
    scan_files: list[EstimateFile]
    unreadable_files: list[EstimateFile]


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
    principle: int
    principle_name: str
    critical: bool = Field(description="the rubric marks this criterion critical")
    syllabi: int = Field(description="syllabi with this finding, either status")
    not_compliant: int = Field(description="of those, how many are Not compliant; the rest are Partially")
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


class CheckModeOption(Schema):
    mode: str = Field(description="standard | single")
    model: str | None
    label: str
    per_syllabus_usd: float = Field(description="checking one syllabus against the criteria in this mode")
    measured: bool = Field(description="true: from recent checks in this mode; false: from list prices")


class CheckModeOut(Schema):
    """How the AI checks syllabi (Administration → AI check), and what each way costs per syllabus."""

    mode: str
    model: str | None
    label: str
    billed: bool
    servers: list[AiServerOut] = Field(description="where the AI of the standard check runs")
    updated_at: datetime | None
    updated_by_name: str | None
    options: list[CheckModeOption]


class CheckModeIn(Schema):
    mode: str = Field(description="standard | single")
    model: str | None = None
