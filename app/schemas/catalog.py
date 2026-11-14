import uuid
from datetime import datetime
from typing import Annotated

from pydantic import Field

from app.enums import AiStatus, EvaluationType, FolderStatus, Role
from app.schemas.common import Schema

# Slugs appear in URLs and in `python -m app.cli add-user --school/--program`, so they stay url-safe.
Slug = Annotated[str, Field(pattern=r"^[a-z0-9][a-z0-9-]{0,59}$")]
Name = Annotated[str, Field(min_length=1, max_length=200)]


class SchoolIn(Schema):
    slug: Slug
    name: Name


class SchoolOut(Schema):
    id: uuid.UUID
    slug: str
    name: str


class ProgramIn(Schema):
    slug: Slug
    name: Name
    school_id: uuid.UUID
    degree_level: Annotated[str, Field(min_length=1, max_length=40)]
    # Criterion 3.4 stays "Cannot verify" for a program until its passport text is here.
    passport_text: str | None = None


class ProgramOut(Schema):
    id: uuid.UUID
    slug: str
    name: str
    school_id: uuid.UUID
    degree_level: str
    has_passport: bool


class CourseIn(Schema):
    slug: Slug
    name: Name
    program_id: uuid.UUID
    credits: Annotated[int, Field(gt=0, le=60)]
    lead_id: uuid.UUID
    # The lead is always an instructor too, so listing them here again is optional.
    instructor_ids: list[uuid.UUID] = []


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
    # megapro_opac, megapro_api or off (integrations/library.yaml)
    library: str = "off"


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
    drive_folder_id: str | None = Field(None, description="the folder's Drive id, or a pasted Drive link")
    status: FolderStatus = FolderStatus.not_started


class FolderPatch(Schema):
    path: str | None = None
    semester: str | None = None
    drive_folder_id: str | None = Field(None, description="the folder's Drive id, or a pasted Drive link")
    status: FolderStatus | None = None


class FolderOut(Schema):
    id: uuid.UUID
    school_id: uuid.UUID
    path: str
    semester: str
    drive_folder_id: str | None
    last_synced_at: datetime | None
    files: int
    checked: int
    status: FolderStatus
    declared_at: datetime | None
    declared_by: str | None = Field(None, description="who last set the status")


class CollectionFolderOut(Schema):
    """One Drive folder a School registered for the semester, with what the School says about it."""

    folder_id: uuid.UUID
    path: str
    drive_folder_id: str | None
    drive_link: str | None
    status: FolderStatus
    found: int
    checked: int
    at_head: int = Field(description="its syllabi waiting on a Head of Program")
    at_kok: int = Field(description="its syllabi waiting on KOK")
    approved: int
    declared_at: datetime | None
    declared_by: str | None
    last_synced_at: datetime | None
    read_at: datetime | None = Field(
        default=None, description="when the app last tried to read it from Drive"
    )
    read_error: str | None = Field(default=None, description="why that read failed; None when it was read")
    with_findings: int = Field(default=0, description="its checked syllabi with at least one finding")
    with_critical: int = Field(
        default=0, description="its checked syllabi with at least one critical finding"
    )
    cannot_check: int = Field(default=0, description="its syllabi the AI could not read")
    processing: int = Field(default=0, description="its syllabi the AI is checking right now")


class ResponsibleOut(Schema):
    """Someone who answers for a School's collection: one of its School administrators."""

    name: str
    email: str


class FolderFileOut(Schema):
    """A syllabus file in a course folder, and the syllabus the app keeps for it once it has read it."""

    drive_id: str
    name: str
    path: str
    drive_link: str | None
    modified_at: datetime | None
    excluded: bool = Field(description="the School excluded it: it is not read in")
    syllabus_id: uuid.UUID | None = Field(description="None: the app has not read this file yet")
    ai_status: AiStatus | None
    issues: int | None
    critical_issues: int | None = Field(default=None, description="of its issues, how many are critical")
    major_issues: int | None = Field(default=None, description="of its issues, how many are major")


class CourseFolderOut(Schema):
    name: str
    drive_id: str | None
    excluded: bool = Field(description="the School excluded the whole course folder: none of it is read in")
    files: list[FolderFileOut] = Field(description="empty: nobody has put a syllabus in this course folder")


class FolderContentsOut(Schema):
    """What is inside a School's Drive folder right now, read live from Drive."""

    folder_id: uuid.UUID
    listed_at: datetime = Field(description="when this listing was taken from Drive")
    courses: list[CourseFolderOut]
    skipped: list[dict[str, str]] = Field(description="files the app does not read, with the reason")


class DriveItemIn(Schema):
    """A file or course folder inside a School folder."""

    drive_id: str
    name: str | None = Field(default=None, description="a course folder's name, to hide what it brought in")


class ExclusionIn(Schema):
    """What the School changed in one go: items to stop reading in, and items to read in again."""

    exclude: list[DriveItemIn] = Field(default_factory=list)
    include: list[DriveItemIn] = Field(default_factory=list)


class ExclusionOut(Schema):
    excluded_drive_ids: list[str]


class FolderReadOut(Schema):
    """The outcome of reading one School folder into the app."""

    folder_id: uuid.UUID
    disciplines: int = Field(description="course folders found")
    files: int = Field(description="syllabus files found")
    added: int
    updated: int
    unchanged: int
    missing: int = Field(description="files that left the folder since the last read")
    error: str | None


class CollectionSchoolOut(Schema):
    """One School's collection for a semester: every Drive folder it registered, what it says about each,
    and what was actually found. A School may register as many folders as it likes, or none."""

    school_id: uuid.UUID
    school_name: str
    folders: list[CollectionFolderOut]
    found: int = Field(
        description="syllabi of the School's courses this semester, whichever way they arrived"
    )
    checked: int
    at_head: int
    at_kok: int
    approved: int
    checking_total: int = Field(description="its syllabi in a checking session running now")
    checking_done: int = Field(description="of those, how many the session has finished")
    with_findings: int = Field(default=0, description="checked syllabi with at least one issue")
    with_critical: int = Field(default=0, description="checked syllabi with at least one critical finding")
    cannot_check: int = Field(default=0, description="syllabi the AI could not read")
    remaining: int = Field(
        default=0,
        description="syllabi a check of only the unchecked would take: not checked, not in progress",
    )
    responsible: list[ResponsibleOut] = Field(
        default_factory=list, description="the School's administrators, who answer for its collection"
    )
    checking_session_ids: list[uuid.UUID] = Field(
        default_factory=list, description="running sessions that cover this School, to pause from its row"
    )


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
