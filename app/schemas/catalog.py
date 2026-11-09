import uuid
from datetime import datetime

from pydantic import Field

from app.enums import EvaluationType, Role
from app.schemas.common import Schema


class SchoolOut(Schema):
    id: uuid.UUID
    slug: str
    name: str


class ProgramOut(Schema):
    id: uuid.UUID
    slug: str
    name: str
    school_id: uuid.UUID
    degree_level: str
    has_passport: bool


class CourseOut(Schema):
    id: uuid.UUID
    slug: str
    name: str
    program_id: uuid.UUID
    credits: int
    # None for a course read from Google Drive (services/drive_catalog.py).
    lead_id: uuid.UUID | None
    instructor_ids: list[uuid.UUID]


class ProfessorOut(Schema):
    id: uuid.UUID
    name: str
    title: str | None


class RoleOut(Schema):
    role: Role
    school_id: uuid.UUID | None
    program_id: uuid.UUID | None


class MeOut(Schema):
    id: uuid.UUID
    email: str
    name: str
    title: str | None
    roles: list[RoleOut]
    led_course_ids: list[uuid.UUID]
    taught_course_ids: list[uuid.UUID]


class IntegrationsOut(Schema):
    auth: str
    drive: str
    email: str
    llm: str


class CriterionOut(Schema):
    id: uuid.UUID
    code: str
    name: str
    evaluation_type: EvaluationType
    required_context: str | None
    guidance: str | None


class PrincipleOut(Schema):
    number: int
    name: str
    criteria: list[CriterionOut]


class RubricOut(Schema):
    id: uuid.UUID
    code: str
    title: str
    imported_at: datetime
    principles: list[PrincipleOut]


class FolderIn(Schema):
    path: str
    semester: str
    drive_folder_id: str | None = None


class FolderPatch(Schema):
    path: str | None = None
    semester: str | None = None
    drive_folder_id: str | None = None


class FolderOut(Schema):
    id: uuid.UUID
    school_id: uuid.UUID
    path: str
    semester: str
    drive_folder_id: str | None
    last_synced_at: datetime | None
    files: int
    checked: int


class DriveStatusOut(Schema):
    """Google Drive connection as the app uses it now. The key itself is never returned."""

    provider: str = Field(description="google, or stub (files stay on the server)")
    source: str = Field(description="app: the key uploaded in the app; env: the server's .env")
    credentials_type: str | None = Field(description="service_account or authorized_user (uploaded key)")
    client_email: str | None = Field(description="share Drive folders with this email")
    client_id: str | None = Field(description="for domain-wide delegation in the Google Admin console")
    project_id: str | None
    env_credentials: bool = Field(description="a key from .env is in use")
    impersonate_user: str
    shared_folder_id: str
    default_folder_id: str
    read_only: bool
    updated_by: str | None
    updated_at: datetime | None


class DriveSettingsIn(Schema):
    shared_folder_id: str | None = Field(default=None, description="folder id or its link; empty clears it")
    impersonate_user: str | None = Field(default=None, description="Workspace user to act as; empty: none")
    read_only: bool | None = None


class DriveCheckIn(Schema):
    folder_ids: list[str] = Field(
        default_factory=list, description="default: the shared folder, the default one and every School's"
    )
    write: bool = Field(default=False, description="also upload a small test file (and delete it)")


class DriveAccountCheck(Schema):
    ok: bool
    error: str | None
    type: str | None
    email: str | None
    project_id: str | None
    acting_as: str


class DriveFolderCheck(Schema):
    id: str
    ok: bool
    error: str | None
    name: str | None
    location: str | None
    can_upload: bool | None
    items: int | None
    sample: list[str]
    warnings: list[str]
    write_test: str | None


class DriveCheckOut(Schema):
    provider: str
    source: str
    read_only: bool
    account: DriveAccountCheck
    folders: list[DriveFolderCheck]
    ok: bool
    checked_at: datetime


class DriveSetupIn(Schema):
    semester: str
    create: bool = True
    dry_run: bool = False


class DriveSetupLink(Schema):
    school: str
    action: str = Field(description="found, created, would create, or missing")
    drive_folder_id: str | None


class DriveSetupOut(Schema):
    shared_folder_id: str
    semester: str
    dry_run: bool
    links: list[DriveSetupLink]
    folders_created: int
    folders_updated: int


class DriveSkipped(Schema):
    path: str
    reason: str


class DriveSyncIn(Schema):
    school_id: uuid.UUID | None = Field(default=None, description="only this School's folders; default: all")


class DriveSyncReport(Schema):
    disciplines: int
    files: int
    added: int
    updated: int
    unchanged: int
    missing: int
    skipped: list[DriveSkipped]
    error: str | None
    synced_at: datetime


class DriveSyncFolder(Schema):
    folder_id: uuid.UUID
    school_id: uuid.UUID
    school_name: str
    semester: str
    path: str
    drive_folder_id: str
    syllabi: int = Field(description="syllabi of this folder in the app, without those that left Drive")
    last_sync: DriveSyncReport | None


class DriveSyncOut(Schema):
    running: bool
    folders: list[DriveSyncFolder]
