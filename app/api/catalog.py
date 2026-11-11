import asyncio
import re
import uuid
from datetime import UTC, datetime

import sqlalchemy as sa
from fastapi import APIRouter, HTTPException, status

from app.auth.rbac import forbidden, manages_school
from app.config import get_settings
from app.deps import CurrentUser, DbSession
from app.enums import AiStatus, FolderStatus, Role
from app.models import Course, Program, RubricVersion, School, Syllabus, SyllabusVersion, UploadFolder, User
from app.models.org import course_instructors
from app.schemas.catalog import (
    CollectionSchoolOut,
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
from app.services import drive_admin
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
    if not principal.university_wide:
        raise forbidden("Only an administrator, the Provost Office or KOK can add a School")
    school = School(slug=body.slug, name=body.name.strip())
    session.add(school)
    await _commit(session, "A School with this slug already exists")
    return school


@router.post("/programs", response_model=ProgramOut, status_code=status.HTTP_201_CREATED)
async def create_program(body: ProgramIn, principal: CurrentUser, session: DbSession):
    # The Provost Office, KOK, or the administrator of the School the program belongs to.
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
    may_add = manages_school(principal, program.school_id) or (
        principal.has(Role.program_head) and program.id in principal.program_ids
    )
    if not may_add:
        raise forbidden(
            "Only the Provost Office, KOK, the School administrator or the program head can add a course"
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
    """What each connection is backed by, for the Settings screen: read from the settings (and for Drive the
    key uploaded in the app), so the screen cannot drift from what the app really does."""
    s = get_settings()
    return IntegrationsOut(
        auth=s.auth_provider,  # stub, google or http
        drive=(await drive_config.refresh()).provider,  # stub or google
        email=s.email_backend,
        llm=s.llm_provider,
    )


# A School registers its folder by pasting the link out of the Drive address bar, so the id is taken from
# whatever they paste: ".../folders/<id>", an "id=<id>" query, or the bare id itself.
_DRIVE_ID = re.compile(r"(?:/folders/|[?&]id=)([A-Za-z0-9_-]{10,})")


def drive_folder_id_from(text: str) -> str:
    cleaned = text.strip()
    found = _DRIVE_ID.search(cleaned)
    if found:
        return found.group(1)
    # A bare id: anything else is a link we failed to read, and saying so beats storing it and finding out later.
    if re.fullmatch(r"[A-Za-z0-9_-]{10,}", cleaned):
        return cleaned
    raise HTTPException(
        status.HTTP_422_UNPROCESSABLE_CONTENT,
        "This does not look like a Drive folder link. Open the folder in Google Drive and copy its address.",
    )


def _found(folder: UploadFolder, found: int) -> int | None:
    """How many syllabi the School still owes, when it said how many to expect."""
    return None if folder.expected_count is None else max(folder.expected_count - found, 0)


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
        status=folder.status,
        expected_count=folder.expected_count,
        declared_at=folder.declared_at,
        declared_by=folder.declared_by.name if folder.declared_by else None,
        missing=_found(folder, files),
    )


async def _checked_drive_folder(drive_folder_id: str | None) -> str | None:
    cleaned = (drive_folder_id or "").strip()
    if not cleaned:
        return None
    folder_id = drive_folder_id_from(cleaned)
    await drive_config.refresh()
    if not await asyncio.to_thread(drive.get_drive().folder_accessible, folder_id):
        # Much the commonest cause is a folder that was never shared, which otherwise reads as an empty one.
        account = (await drive_admin.status()).get("client_email")
        share_with = f" Share it with {account}." if account else ""
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            f"This Drive folder cannot be opened.{share_with}",
        )
    return folder_id


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
        status=body.status,
        expected_count=body.expected_count,
        declared_at=datetime.now(UTC),
        declared_by_id=principal.id,
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
    # Null clears the expected count, which is how a School withdraws a figure it should not have given.
    if "expected_count" in body.model_fields_set:
        folder.expected_count = body.expected_count
    said = {"status", "expected_count"} & body.model_fields_set
    for key, value in body.model_dump(exclude_none=True, exclude={"drive_folder_id", "expected_count"}).items():
        setattr(folder, key, value.strip() if isinstance(value, str) else value)
    # Record who last spoke for the School, so the Provost Office can see whose claim it is reading.
    if said:
        folder.declared_at = datetime.now(UTC)
        folder.declared_by_id = principal.id
    try:
        await session.commit()
    except sa.exc.IntegrityError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, "This folder already exists") from exc
    return await _folder_out(session, folder)


@router.get("/collection", response_model=list[CollectionSchoolOut])
async def collection(semester: str, principal: CurrentUser, session: DbSession):
    """Where each School stands in collecting its syllabi for a semester.

    A School that has registered no folder still gets a row, because a School that has done nothing is
    precisely what this screen exists to show; everything derived from the files alone can only ever
    describe the Schools that already uploaded something.
    """
    schools = (await session.scalars(sa.select(School).order_by(School.name))).all()
    if not principal.university_wide:
        schools = [s for s in schools if s.id in principal.school_ids]
    folders = {
        f.school_id: f
        for f in await session.scalars(
            sa.select(UploadFolder).where(UploadFolder.semester == semester).order_by(UploadFolder.path)
        )
    }
    found_by_school = dict(
        (
            await session.execute(
                sa.select(Program.school_id, sa.func.count(Syllabus.id))
                .join(Course, Course.program_id == Program.id)
                .join(Syllabus, sa.and_(Syllabus.course_id == Course.id, Syllabus.semester == semester))
                .group_by(Program.school_id)
            )
        ).all()
    )
    checked_by_school = dict(
        (
            await session.execute(
                sa.select(Program.school_id, sa.func.count(Syllabus.id))
                .join(Course, Course.program_id == Program.id)
                .join(Syllabus, sa.and_(Syllabus.course_id == Course.id, Syllabus.semester == semester))
                .where(Syllabus.ai_status.in_([AiStatus.checked, AiStatus.needs_revision]))
                .group_by(Program.school_id)
            )
        ).all()
    )
    rows = []
    for school in schools:
        folder = folders.get(school.id)
        found = found_by_school.get(school.id, 0)
        drive_id = folder.drive_folder_id if folder else None
        rows.append(
            CollectionSchoolOut(
                school_id=school.id,
                school_name=school.name,
                folder_id=folder.id if folder else None,
                path=folder.path if folder else None,
                drive_folder_id=drive_id,
                drive_link=f"https://drive.google.com/drive/folders/{drive_id}" if drive_id else None,
                status=folder.status if folder else FolderStatus.not_started,
                expected_count=folder.expected_count if folder else None,
                found=found,
                checked=checked_by_school.get(school.id, 0),
                missing=_found(folder, found) if folder else None,
                declared_at=folder.declared_at if folder else None,
                declared_by=folder.declared_by.name if folder and folder.declared_by else None,
                last_synced_at=await session.scalar(
                    sa.select(sa.func.max(SyllabusVersion.drive_synced_at)).where(
                        SyllabusVersion.folder_id == folder.id
                    )
                )
                if folder
                else None,
            )
        )
    return rows
