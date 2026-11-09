"""MongoDB documents (Beanie): extracted syllabus content, AI results, proposals, wizard drafts, LLM traces."""

from datetime import UTC, datetime
from typing import Literal
from uuid import UUID, uuid4

from beanie import Document
from pydantic import BaseModel, Field
from pymongo import ASCENDING, IndexModel

from app.ai.types import CriterionResultData, ExtractedContent, ProposalDraft, TechnicalReport


def _now() -> datetime:
    return datetime.now(UTC)


class ExtractedDocument(Document):
    version_id: UUID
    content: ExtractedContent
    created_at: datetime = Field(default_factory=_now)

    class Settings:
        name = "extracted_documents"
        indexes = [IndexModel([("version_id", ASCENDING)], unique=True)]


class AssessmentResultDoc(Document):
    run_id: UUID
    version_id: UUID
    technical: TechnicalReport
    results: list[CriterionResultData]
    created_at: datetime = Field(default_factory=_now)

    class Settings:
        name = "assessment_results"
        indexes = [IndexModel([("run_id", ASCENDING)], unique=True), IndexModel([("version_id", ASCENDING)])]


class ProposalItem(ProposalDraft):
    id: str = Field(default_factory=lambda: uuid4().hex[:8])
    decision: Literal["pending", "accepted", "rejected"] = "pending"
    edited: bool = False


class ChangeProposalDoc(Document):
    run_id: UUID
    version_id: UUID
    syllabus_id: UUID
    items: list[ProposalItem]
    applied_version_id: UUID | None = None
    created_by: UUID
    created_at: datetime = Field(default_factory=_now)

    class Settings:
        name = "change_proposals"
        indexes = [IndexModel([("run_id", ASCENDING)])]


class WizardDraft(Document):
    owner_id: UUID
    course_id: UUID | None = None
    title: str = ""
    language: str = "English"
    semester: str = "Fall 2026"
    degree_level: str = "Bachelor"
    fields: dict[str, str] = Field(default_factory=dict)
    step: int = 0
    syllabus_id: UUID | None = None
    created_at: datetime = Field(default_factory=_now)
    updated_at: datetime = Field(default_factory=_now)

    class Settings:
        name = "wizard_drafts"
        indexes = [IndexModel([("owner_id", ASCENDING)])]


class AssistantMessage(BaseModel):
    role: Literal["user", "assistant"]
    section: str
    content: str
    created_at: datetime = Field(default_factory=_now)


class AssistantThread(Document):
    draft_id: str
    messages: list[AssistantMessage] = Field(default_factory=list)

    class Settings:
        name = "assistant_threads"
        indexes = [IndexModel([("draft_id", ASCENDING)], unique=True)]


class LlmTrace(Document):
    purpose: str
    run_id: UUID | None = None
    model: str
    messages: list[dict]
    response: str | None = None
    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    duration_ms: int
    # Time spent waiting for a free request slot (LLM_MAX_CONCURRENCY) before the request was sent.
    queued_ms: int | None = None
    error: str | None = None
    created_at: datetime = Field(default_factory=_now)

    class Settings:
        name = "llm_traces"


class DriveSettingsDoc(Document):
    """Google Drive settings saved in the app (one document); see app/storage/drive_config.py.
    The key itself is a file under SECRETS_ROOT, never stored here."""

    credentials_saved: bool = False
    credentials_type: str | None = None
    client_email: str | None = None
    client_id: str | None = None
    project_id: str | None = None
    impersonate_user: str = ""
    shared_folder_id: str = ""
    read_only: bool = True
    updated_by: str | None = None
    updated_at: datetime = Field(default_factory=_now)

    class Settings:
        name = "drive_settings"


DOCUMENT_MODELS = [
    ExtractedDocument,
    AssessmentResultDoc,
    ChangeProposalDoc,
    WizardDraft,
    AssistantThread,
    LlmTrace,
    DriveSettingsDoc,
]
