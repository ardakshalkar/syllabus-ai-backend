import asyncio
import uuid

import sqlalchemy as sa
from fastapi import APIRouter, HTTPException, status

from app.auth.rbac import forbidden
from app.config import get_settings
from app.deps import CurrentUser, DbSession
from app.enums import AiStatus, Role
from app.models import Course, Program, RubricVersion, School, Syllabus, UploadFolder, User
from app.models.org import course_instructors
from app.schemas.catalog import (
    CourseIn,
    CourseOut,
    FolderIn,
    FolderOut,
    FolderPatch,
    IntegrationsOut,
    ProfessorOut,
    ProgramIn,
    ProgramOut,
    RubricOut,
    SchoolIn,
    SchoolOut,
)
from app.services.sessions import ensure_can_manage
from app.storage import drive, drive_config

router = APIRouter(tags=["catalog"])


def _program_out(program: Program) -> ProgramOut:
    return ProgramOut(
        id=program.id,
        slug=program.slug,
        name=program.name,
        school_id=program.school_id,
        degree_level=program.degree_level,
        has_passport=bool(program.passport_text),
    )


@router.get("/schools", response_model=list[SchoolOut])
async def schools(_: CurrentUser, session: DbSession):
    return list(await session.scalars(sa.select(School).order_by(School.name)))


@router.get("/programs", response_model=list[ProgramOut])
async def programs(_: CurrentUser, session: DbSession, school_id: uuid.UUID | None = None):
    query = sa.select(Program).order_by(Program.name)
    if school_id:
        query = query.where(Program.school_id == school_id)
    # has_passport is derived, not a column, so model_validate on the ORM object cannot supply it.
    return [_program_out(p) for p in await session.scalars(query)]


@router.get("/courses", response_model=list[CourseOut])
async def courses(_: CurrentUser, session: DbSession, program_id: uuid.UUID | None = None):
    query = sa.select(Course).order_by(Course.name)
    if program_id:
        query = query.where(Course.program_id == program_id)
    return [
        CourseOut(
            id=c.id,
            slug=c.slug,
            name=c.name,
            program_id=c.program_id,
            credits=c.credits,
            lead_id=c.lead_id,
            instructor_ids=[u.id for u in c.instructors],
        )
        for c in await session.scalars(query)
    ]


@router.get("/professors", response_model=list[ProfessorOut])
async def professors(_: CurrentUser, session: DbSession):
    teaching = sa.select(course_instructors.c.user_id).union(sa.select(Course.lead_id))
    return list(await session.scalars(sa.select(User).where(User.id.in_(teaching)).order_by(User.name)))


@router.get("/rubric", response_model=RubricOut)
async def rubric(_: CurrentUser, session: DbSession, code: str | None = None):
    query = sa.select(RubricVersion)
    query = (
        query.where(RubricVersion.code == code) if code else query.where(RubricVersion.is_active.is_(True))
    )
    found = await session.scalar(query)
    if found is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Rubric not found")
    return found


async def _commit(session: DbSession, conflict: str) -> None:
    """Slugs are unique in the database; turn the violation into a 409 rather than a 500."""
    try:
        await session.commit()
    except sa.exc.IntegrityError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, conflict) from exc


@router.post("/schools", response_model=SchoolOut, status_code=status.HTTP_201_CREATED)
async def create_school(body: SchoolIn, principal: CurrentUser, session: DbSession):
    if not principal.has(Role.provost):
        raise forbidden("Only the Provost Office can add a School")
    school = School(slug=body.slug, name=body.name.strip())
    session.add(school)
    await _commit(session, "A School with this slug already exists")
    return school


@router.post("/programs", response_model=ProgramOut, status_code=status.HTTP_201_CREATED)
async def create_program(body: ProgramIn, principal: CurrentUser, session: DbSession):
    # The Provost Office, or the administrator of the School the program belongs to.
    ensure_can_manage(principal, body.school_id)
    if await session.get(School, body.school_id) is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "School not found")
    program = Program(
        slug=body.slug,
        name=body.name.strip(),
        school_id=body.school_id,
        degree_level=body.degree_level.strip(),
        passport_text=body.passport_text,
    )
    session.add(program)
    await _commit(session, "A program with this slug already exists")
    return _program_out(program)


@router.post("/courses", response_model=CourseOut, status_code=status.HTTP_201_CREATED)
async def create_course(body: CourseIn, principal: CurrentUser, session: DbSession):
    program = await session.get(Program, body.program_id)
    if program is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Program not found")
    may_add = (
        principal.has(Role.provost)
        or (principal.has(Role.school_admin) and program.school_id in principal.school_ids)
        or (principal.has(Role.program_head) and program.id in principal.program_ids)
    )
    if not may_add:
        raise forbidden(
            "Only the Provost Office, the School administrator or the program head can add a course"
        )
    # The lead teaches the course, so they join the instructors whether or not they were listed.
    wanted = list(dict.fromkeys([body.lead_id, *body.instructor_ids]))
    people = {u.id: u for u in await session.scalars(sa.select(User).where(User.id.in_(wanted)))}
    if missing := [str(i) for i in wanted if i not in people]:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"Unknown user: {', '.join(missing)}")
    course = Course(
        slug=body.slug,
        name=body.name.strip(),
        program_id=body.program_id,
        credits=body.credits,
        lead_id=body.lead_id,
    )
    course.instructors = [people[i] for i in wanted]
    session.add(course)
    await _commit(session, "A course with this slug already exists")
    return CourseOut(
        id=course.id,
        slug=course.slug,
        name=course.name,
        program_id=course.program_id,
        credits=course.credits,
        lead_id=course.lead_id,
        instructor_ids=wanted,
    )


@router.get("/integrations", response_model=IntegrationsOut)
async def integrations(_: CurrentUser) -> IntegrationsOut:
    s = get_settings()
    return IntegrationsOut(
        # Google OIDC is the real sign-in. Without client credentials only dev-login is left,
        # which is why the frontend still offers its demo user switcher.
        auth="http" if s.google_client_id and s.google_client_secret else "stub",
        # Drive is not connected: files are uploaded through the API to FILES_ROOT and a School's
        # folder is a logical path (models.org.UploadFolder), not a Drive folder.
        drive="stub",
        email=s.email_backend,
        llm=s.llm_provider,
    )


async def _folder_out(session: DbSession, folder: UploadFolder) -> FolderOut:
    files, checked = (
        await session.execute(
            sa.select(
                sa.func.count(Syllabus.id),
                sa.func.count(Syllabus.id).filter(
                    Syllabus.ai_status.in_([AiStatus.checked, AiStatus.needs_revision])
                ),
            ).where(Syllabus.folder_id == folder.id)
        )
    ).one()
    last_synced_at = await session.scalar(
        sa.select(sa.func.max(SyllabusVersion.drive_synced_at)).where(SyllabusVersion.folder_id == folder.id)
    )
    return FolderOut(
        id=folder.id,
        school_id=folder.school_id,
        path=folder.path,
        semester=folder.semester,
        drive_folder_id=folder.drive_folder_id,
        last_synced_at=last_synced_at,
        files=files,
        checked=checked,
    )


async def _checked_drive_folder(drive_folder_id: str | None) -> str | None:
    cleaned = (drive_folder_id or "").strip()
    if not cleaned:
        return None
    await drive_config.refresh()
    if not await asyncio.to_thread(drive.get_drive().folder_accessible, cleaned):
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "This Drive folder is not accessible")
    return cleaned


@router.get("/schools/{school_id}/folders", response_model=list[FolderOut])
async def list_folders(school_id: uuid.UUID, principal: CurrentUser, session: DbSession):
    if not (principal.university_wide or school_id in principal.school_ids):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "School not found")
    folders = await session.scalars(
        sa.select(UploadFolder).where(UploadFolder.school_id == school_id).order_by(UploadFolder.path)
    )
    return [await _folder_out(session, f) for f in folders]


@router.post("/schools/{school_id}/folders", response_model=FolderOut, status_code=status.HTTP_201_CREATED)
async def create_folder(school_id: uuid.UUID, body: FolderIn, principal: CurrentUser, session: DbSession):
    ensure_can_manage(principal, school_id)
    if await session.get(School, school_id) is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "School not found")
    folder = UploadFolder(
        school_id=school_id,
        path=body.path.strip(),
        semester=body.semester,
        drive_folder_id=await _checked_drive_folder(body.drive_folder_id),
    )
    session.add(folder)
    try:
        await session.commit()
    except sa.exc.IntegrityError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, "This folder already exists") from exc
    return await _folder_out(session, folder)


@router.patch("/folders/{folder_id}", response_model=FolderOut)
async def update_folder(folder_id: uuid.UUID, body: FolderPatch, principal: CurrentUser, session: DbSession):
    folder = await session.get(UploadFolder, folder_id)
    if folder is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Folder not found")
    ensure_can_manage(principal, folder.school_id)
    # Sending null for drive_folder_id disconnects the folder; other fields ignore null.
    if "drive_folder_id" in body.model_fields_set:
        folder.drive_folder_id = await _checked_drive_folder(body.drive_folder_id)
    for key, value in body.model_dump(exclude_none=True, exclude={"drive_folder_id"}).items():
        setattr(folder, key, value.strip() if isinstance(value, str) else value)
    try:
        await session.commit()
    except sa.exc.IntegrityError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, "This folder already exists") from exc
    return await _folder_out(session, folder)
