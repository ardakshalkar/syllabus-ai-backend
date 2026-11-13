"""Load the prototype's demo data (lib/data.ts) with synthetic documents and run their AI checks.

Run: `python -m app.cli seed`. Idempotent: does nothing if the schools already exist.
Demo users (bearer token `stub:<email>`): provost@demo.narxoz.kz, kok@demo.narxoz.kz,
admin.digital@demo.narxoz.kz, head.cs@demo.narxoz.kz and the professors below (e.g. shalkar@demo.narxoz.kz).
"""

import logging
from pathlib import Path

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.pg import session_scope
from app.enums import ApprovalStage, Role, Trigger, VersionSource
from app.models import Course, Program, RoleAssignment, RubricVersion, School, Syllabus, UploadFolder, User
from app.services import checks, syllabi
from app.services.rubric import import_rubric
from seed.samples import BROKEN_PDF, sample_syllabus

log = logging.getLogger(__name__)

RUBRIC = Path(__file__).resolve().parent.parent / "rubric" / "2026.1.yaml"
SEMESTER = "Fall 2026"
DOMAIN = "demo.narxoz.kz"

SCHOOLS = [
    ("digital", "School of Digital Technologies"),
    ("economics", "School of Economics and Management"),
    ("arts", "School of Arts and Social Sciences"),
]
PROGRAMS = [
    ("cs", "Computer Science", "digital"),
    ("is", "Information Systems", "digital"),
    ("finance", "Finance", "economics"),
    ("economics", "Economics", "economics"),
    ("history", "History", "arts"),
]
PROFESSORS = [
    ("shalkar", "A. Shalkarbayuly", "Associate Professor"),
    ("karimova", "M. Karimova", "Professor"),
    ("sultanov", "D. Sultanov", "Senior Lecturer"),
    ("lee", "J. Lee", "Associate Professor"),
    ("abayeva", "L. Abayeva", "Senior Lecturer"),
    ("sadykov", "R. Sadykov", "Lecturer"),
]
COURSES = [
    ("ai", "Artificial Intelligence", "cs", 5, "shalkar", ["shalkar", "lee", "abayeva"]),
    ("corp", "Corporate Finance", "finance", 5, "karimova", ["karimova", "sultanov"]),
    ("intro", "Introduction to Economics", "economics", 4, "sultanov", ["sultanov", "karimova"]),
    ("ml", "Machine Learning", "cs", 5, "lee", ["lee", "shalkar", "sadykov"]),
    ("history", "History of Kazakhstan", "history", 3, "abayeva", ["abayeva"]),
    ("systems", "Information Systems Design", "is", 5, "sadykov", ["sadykov", "shalkar"]),
]
STAFF = [
    ("provost", "Provost Office", Role.provost, None, None),
    ("kok", "KOK Secretary", Role.kok, None, None),
    ("admin.digital", "Digital School Administrator", Role.school_admin, "digital", None),
    ("head.cs", "Head of Computer Science", Role.program_head, None, "cs"),
]
FOLDERS = [
    ("digital", "Academic / Syllabi / 2026 / Fall"),
    ("economics", "Syllabi / Fall-2026"),
    ("arts", "Academic Affairs / Fall 2026"),
]
# course, language, versions, issues in the documents, approval stage to end on, run the check?
SYLLABI = [
    ("ai", "English", 3, True, ApprovalStage.head_review, True),
    ("corp", "Russian", 2, False, ApprovalStage.kok_review, True),
    ("intro", "English", 4, True, ApprovalStage.professor_revised, True),
    ("ml", "English", 2, False, ApprovalStage.draft, False),
    ("history", "Kazakh", 1, False, ApprovalStage.draft, True),
    ("systems", "English", 1, False, ApprovalStage.draft, False),
]


async def _reference_data(session: AsyncSession) -> dict[str, dict]:
    if not await session.scalar(sa.select(RubricVersion.id).limit(1)):
        await import_rubric(session, RUBRIC)
    schools = {slug: School(slug=slug, name=name) for slug, name in SCHOOLS}
    session.add_all(schools.values())
    await session.flush()
    programs = {
        slug: Program(slug=slug, name=name, school_id=schools[school].id, degree_level="Bachelor")
        for slug, name, school in PROGRAMS
    }
    session.add_all(programs.values())
    users = {
        slug: User(
            email=f"{slug}@{DOMAIN}", name=name, title=title, roles=[RoleAssignment(role=Role.professor)]
        )
        for slug, name, title in PROFESSORS
    }
    await session.flush()
    for slug, name, role, school, program in STAFF:
        users[slug] = User(
            email=f"{slug}@{DOMAIN}",
            name=name,
            roles=[
                RoleAssignment(
                    role=role,
                    school_id=schools[school].id if school else None,
                    program_id=programs[program].id if program else None,
                )
            ],
        )
    session.add_all(users.values())
    await session.flush()
    courses = {
        slug: Course(
            slug=slug,
            name=name,
            program_id=programs[program].id,
            credits=credits,
            lead_id=users[lead].id,
            instructors=[users[i] for i in instructors],
        )
        for slug, name, program, credits, lead, instructors in COURSES
    }
    session.add_all(courses.values())
    folders = {
        school: UploadFolder(
            school_id=schools[school].id, path=path, semester=SEMESTER, drive_folder_id=f"demo-{school}"
        )
        for school, path in FOLDERS
    }
    session.add_all(folders.values())
    await session.flush()
    return {"schools": schools, "programs": programs, "users": users, "courses": courses, "folders": folders}


async def run() -> None:
    async with session_scope() as session:
        if await session.scalar(sa.select(School.id).limit(1)):
            log.info("demo data already present; nothing to do")
            return
        ref = await _reference_data(session)

        program_of = {course: program for course, _, program, *_ in COURSES}
        school_of = {program: school for program, _, school in PROGRAMS}
        to_check = []
        for slug, language, versions, with_issues, _, check in SYLLABI:
            course = ref["courses"][slug]
            folder = ref["folders"][school_of[program_of[slug]]]
            syllabus = await syllabi.create_syllabus(
                session,
                course=course,
                professor_id=course.lead_id,
                language=language,
                semester=SEMESTER,
                folder_id=folder.id,
            )
            for n in range(1, versions + 1):
                broken = slug == "history"
                version = await syllabi.add_version(
                    session,
                    syllabus,
                    filename=f"syllabus_v{n}.pdf" if broken else f"syllabus_v{n}.docx",
                    data=BROKEN_PDF if broken else sample_syllabus(course.name, with_issues=with_issues),
                    user_id=course.lead_id,
                    source=VersionSource.seed,
                )
            if check:
                run_ = await checks.create_run(session, version, trigger=Trigger.provost_session)
                to_check.append((syllabus.id, run_.id))

    for _, run_id in to_check:
        await checks.execute_run(run_id)

    async with session_scope() as session:
        for slug, _, _, _, stage, _ in SYLLABI:
            course_id = await session.scalar(sa.select(Course.id).where(Course.slug == slug))
            seeded = await session.scalar(sa.select(Syllabus).where(Syllabus.course_id == course_id))
            assert seeded is not None
            if stage != ApprovalStage.draft and seeded.approval_stage != ApprovalStage.draft:
                seeded.approval_stage = stage
    log.info("demo data loaded: %d syllabi, %d checked", len(SYLLABI), len(to_check))
