import asyncio
import re
import uuid
from datetime import UTC, datetime

import sqlalchemy as sa
from fastapi import APIRouter, HTTPException, status

from app.auth.rbac import forbidden, manages_school
from app.config import get_settings
from app.deps import CurrentUser, DbSession
from app.enums import AiStatus, ApprovalStage, Role, SessionItemStatus, SessionStatus
from app.models import (
    CheckSession,
    Course,
    Program,
    RubricVersion,
    School,
    SessionItem,
    Syllabus,
    SyllabusVersion,
    UploadFolder,
    User,
)
from app.models.org import RoleAssignment, course_instructors
from app.schemas.catalog import (
    CollectionFolderOut,
    CollectionSchoolOut,
    CourseIn,
    CourseOut,
    ExclusionIn,
    ExclusionOut,
    FolderContentsOut,
    FolderIn,
    FolderOut,
    FolderPatch,
    FolderReadOut,
    IntegrationsOut,
    ProfessorOut,
    ProgramIn,
    ProgramOut,
    ResponsibleOut,
    RubricOut,
    SchoolIn,
    SchoolOut,
)
from app.services import drive_admin, drive_catalog
from app.services.sessions import ensure_can_manage
from app.storage import drive, drive_config
from app.storage.drive import explain_error, share_hint

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
        declared_at=folder.declared_at,
        declared_by=folder.declared_by.name if folder.declared_by else None,
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
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, share_hint(account))
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
        declared_at=datetime.now(UTC),
        declared_by_id=principal.id,
    )
    session.add(folder)
    try:
        await session.commit()
    except sa.exc.IntegrityError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, "This folder already exists") from exc
    return await _folder_out(session, folder)


async def _visible_folder(session, principal, folder_id: uuid.UUID) -> UploadFolder:
    """A folder of a School this person answers for or reads: the university-wide roles, its School's
    administrators, and the Heads of its programs."""
    folder = await session.get(UploadFolder, folder_id)
    if folder is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Folder not found")
    if principal.university_wide or folder.school_id in principal.school_ids:
        return folder
    if principal.program_ids and folder.school_id in set(
        (
            await session.scalars(sa.select(Program.school_id).where(Program.id.in_(principal.program_ids)))
        ).all()
    ):
        return folder
    raise HTTPException(status.HTTP_404_NOT_FOUND, "Folder not found")


@router.get("/folders/{folder_id}/contents", response_model=FolderContentsOut)
async def folder_contents(folder_id: uuid.UUID, principal: CurrentUser, session: DbSession):
    """What is inside the folder in Drive right now: course folders, their files, and which the app has read."""
    folder = await _visible_folder(session, principal, folder_id)
    try:
        return await drive_catalog.folder_contents(session, folder)
    except ValueError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, str(exc)) from exc
    except Exception as exc:
        account = (await drive_admin.status()).get("client_email")
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, explain_error(exc, account)) from exc


@router.post("/folders/{folder_id}/read", response_model=FolderReadOut)
async def read_folder(folder_id: uuid.UUID, principal: CurrentUser, session: DbSession):
    """Read this one folder from Drive now and map its syllabi into the app: a course per syllabus file.
    The outcome, error included, is also kept on the folder for Syllabus collection."""
    folder = await _visible_folder(session, principal, folder_id)
    ensure_can_manage(principal, folder.school_id)
    report = await drive_catalog.sync_folder(folder.id)
    return FolderReadOut(
        folder_id=folder.id,
        disciplines=report.disciplines,
        files=report.files,
        added=report.added,
        updated=report.updated,
        unchanged=report.unchanged,
        missing=report.missing,
        error=report.error,
    )


@router.put("/folders/{folder_id}/exclusions", response_model=ExclusionOut)
async def set_exclusion(folder_id: uuid.UUID, body: ExclusionIn, principal: CurrentUser, session: DbSession):
    """Exclude files and course folders from being read in, or include them again, in one go. Who manages
    the School does."""
    folder = await _visible_folder(session, principal, folder_id)
    ensure_can_manage(principal, folder.school_id)
    ids = list(folder.excluded_drive_ids or [])
    for item, excluded in [(i, True) for i in body.exclude] + [(i, False) for i in body.include]:
        ids = await drive_catalog.set_excluded(session, folder, item.drive_id, item.name, excluded)
    await session.commit()
    return ExclusionOut(excluded_drive_ids=ids)


@router.patch("/folders/{folder_id}", response_model=FolderOut)
async def update_folder(folder_id: uuid.UUID, body: FolderPatch, principal: CurrentUser, session: DbSession):
    folder = await session.get(UploadFolder, folder_id)
    if folder is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Folder not found")
    ensure_can_manage(principal, folder.school_id)
    # Sending null for drive_folder_id disconnects the folder; other fields ignore null.
    if "drive_folder_id" in body.model_fields_set:
        folder.drive_folder_id = await _checked_drive_folder(body.drive_folder_id)
    said = "status" in body.model_fields_set
    for key, value in body.model_dump(exclude_none=True, exclude={"drive_folder_id"}).items():
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

    A School registers as many Drive folders as it likes, so each row carries all of them. A School that
    has registered none still gets a row: that is precisely what this screen exists to show, and anything
    counted from the files alone can only describe the Schools that already uploaded something.
    """
    schools = (await session.scalars(sa.select(School).order_by(School.name))).all()
    if not principal.university_wide:
        # A Head of Program is scoped to a program, not a School, but collection is reported per School:
        # resolve the Schools their programs belong to, so a Head can see where their own stands.
        visible = set(principal.school_ids)
        if principal.program_ids:
            visible |= set(
                (
                    await session.scalars(
                        sa.select(Program.school_id).where(Program.id.in_(principal.program_ids))
                    )
                ).all()
            )
        # A professor reads their School's row too, to know which folder their syllabus belongs in.
        teaching = (
            sa.select(course_instructors.c.course_id)
            .where(course_instructors.c.user_id == principal.id)
            .union(sa.select(Course.id).where(Course.lead_id == principal.id))
        )
        visible |= set(
            (
                await session.scalars(
                    sa.select(Program.school_id)
                    .join(Course, Course.program_id == Program.id)
                    .where(Course.id.in_(teaching))
                )
            ).all()
        )
        schools = [s for s in schools if s.id in visible]

    by_school: dict[uuid.UUID, list[UploadFolder]] = {}
    for f in await session.scalars(
        sa.select(UploadFolder).where(UploadFolder.semester == semester).order_by(UploadFolder.path)
    ):
        by_school.setdefault(f.school_id, []).append(f)

    checked_status = Syllabus.ai_status.in_([AiStatus.checked, AiStatus.needs_revision])
    # How far each syllabus has come: found, checked by the AI, waiting on a Head, waiting on KOK, approved.
    counts = (
        sa.func.count(Syllabus.id),
        sa.func.count(Syllabus.id).filter(checked_status),
        sa.func.count(Syllabus.id).filter(Syllabus.approval_stage == ApprovalStage.head_review),
        sa.func.count(Syllabus.id).filter(Syllabus.approval_stage == ApprovalStage.kok_review),
        sa.func.count(Syllabus.id).filter(Syllabus.approval_stage == ApprovalStage.approved),
    )
    zero = (0, 0, 0, 0, 0)
    # Per folder: the syllabi that arrived through it.
    per_folder = {
        row[0]: tuple(row[1:])
        for row in (
            await session.execute(
                sa.select(Syllabus.folder_id, *counts)
                .where(Syllabus.semester == semester, Syllabus.folder_id.is_not(None))
                .group_by(Syllabus.folder_id)
            )
        ).all()
    }
    synced = dict(
        (
            await session.execute(
                sa.select(SyllabusVersion.folder_id, sa.func.max(SyllabusVersion.drive_synced_at))
                .where(SyllabusVersion.folder_id.is_not(None))
                .group_by(SyllabusVersion.folder_id)
            )
        ).all()
    )
    # Per School: every syllabus of its courses this semester, whichever way it arrived (a folder, or an
    # upload in the app), since both paths are in use.
    per_school = {
        row[0]: tuple(row[1:])
        for row in (
            await session.execute(
                sa.select(Program.school_id, *counts)
                .join(Course, Course.program_id == Program.id)
                .join(Syllabus, sa.and_(Syllabus.course_id == Course.id, Syllabus.semester == semester))
                .group_by(Program.school_id)
            )
        ).all()
    }
    # What the AI made of each School's syllabi, beyond checked: how many have findings, how many it could not read.
    # "Remaining" is counted exactly as a session with only_unchecked selects (app/services/sessions.py),
    # so the number offered beside "Check remaining" is the number that check will take.
    waiting = sa.and_(
        Syllabus.ai_status.not_in([AiStatus.checked, AiStatus.needs_revision, AiStatus.processing]),
        Syllabus.drive_missing_at.is_(None),
    )
    results = {
        school_id: (findings, unreadable, remaining)
        for school_id, findings, unreadable, remaining in (
            await session.execute(
                sa.select(
                    Program.school_id,
                    sa.func.count(Syllabus.id).filter(checked_status, Syllabus.issues > 0),
                    sa.func.count(Syllabus.id).filter(Syllabus.ai_status == AiStatus.cannot_check),
                    sa.func.count(Syllabus.id).filter(waiting),
                )
                .join(Course, Course.program_id == Program.id)
                .join(Syllabus, sa.and_(Syllabus.course_id == Course.id, Syllabus.semester == semester))
                .group_by(Program.school_id)
            )
        ).all()
    }
    # Who answers for each School: its School administrators.
    responsible: dict[uuid.UUID, list[ResponsibleOut]] = {}
    for school_id, name, email in (
        await session.execute(
            sa.select(RoleAssignment.school_id, User.name, User.email)
            .join(User, User.id == RoleAssignment.user_id)
            .where(RoleAssignment.role == Role.school_admin, RoleAssignment.school_id.is_not(None))
            .where(User.is_active.is_(True))
            .order_by(User.name)
        )
    ).all():
        if school_id:
            responsible.setdefault(school_id, []).append(ResponsibleOut(name=name, email=email))
    running: dict[uuid.UUID, set[uuid.UUID]] = {}
    for school_id, session_id in (
        await session.execute(
            sa.select(Program.school_id, SessionItem.session_id)
            .distinct()
            .join(CheckSession, CheckSession.id == SessionItem.session_id)
            .join(Syllabus, Syllabus.id == SessionItem.syllabus_id)
            .join(Course, Course.id == Syllabus.course_id)
            .join(Program, Program.id == Course.program_id)
            .where(CheckSession.status == SessionStatus.running, CheckSession.semester == semester)
        )
    ).all():
        running.setdefault(school_id, set()).add(session_id)
    # A check running now, whichever scope started it, reported as the share of this School it covers.
    checking = {
        school_id: (total, done)
        for school_id, total, done in (
            await session.execute(
                sa.select(
                    Program.school_id,
                    sa.func.count(SessionItem.id),
                    sa.func.count(SessionItem.id).filter(
                        SessionItem.status.in_([SessionItemStatus.done, SessionItemStatus.failed])
                    ),
                )
                .join(CheckSession, CheckSession.id == SessionItem.session_id)
                .join(Syllabus, Syllabus.id == SessionItem.syllabus_id)
                .join(Course, Course.id == Syllabus.course_id)
                .join(Program, Program.id == Course.program_id)
                .where(CheckSession.status == SessionStatus.running, CheckSession.semester == semester)
                .group_by(Program.school_id)
            )
        ).all()
    }

    rows = []
    for school in schools:
        folders = []
        for f in by_school.get(school.id, []):
            found, checked, at_head, at_kok, approved = per_folder.get(f.id, zero)
            folders.append(
                CollectionFolderOut(
                    folder_id=f.id,
                    path=f.path,
                    drive_folder_id=f.drive_folder_id,
                    drive_link=f"https://drive.google.com/drive/folders/{f.drive_folder_id}"
                    if f.drive_folder_id
                    else None,
                    status=f.status,
                    found=found,
                    checked=checked,
                    at_head=at_head,
                    at_kok=at_kok,
                    approved=approved,
                    declared_at=f.declared_at,
                    declared_by=f.declared_by.name if f.declared_by else None,
                    last_synced_at=synced.get(f.id),
                    read_at=f.read_at,
                    read_error=f.read_error,
                )
            )
        found, checked, at_head, at_kok, approved = per_school.get(school.id, zero)
        checking_total, checking_done = checking.get(school.id, (0, 0))
        rows.append(
            CollectionSchoolOut(
                school_id=school.id,
                school_name=school.name,
                folders=folders,
                found=found,
                checked=checked,
                at_head=at_head,
                at_kok=at_kok,
                approved=approved,
                checking_total=checking_total,
                checking_done=checking_done,
                with_findings=results.get(school.id, (0, 0, 0))[0],
                cannot_check=results.get(school.id, (0, 0, 0))[1],
                remaining=results.get(school.id, (0, 0, 0))[2],
                responsible=responsible.get(school.id, []),
                checking_session_ids=sorted(running.get(school.id, set())),
            )
        )
    return rows
