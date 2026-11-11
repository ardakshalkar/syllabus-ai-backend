"""The University's Schools and test users for every role. Not migrations: CLI commands, idempotent.

`python -m app.cli setup-schools` creates the four Schools, each with one Program (Google Drive has no
programs, but the Head of Program review step and role hang on one).

`python -m app.cli bootstrap` does both (on a server: SEED_TEST_USERS=true runs it at start-up).

`python -m app.cli add-test-users [--password]` adds, for trying the app as every role (with ENV=prod only
with --allow-prod --password; with a password they sign in on the email + password form):
  superadmin@…           System administrator: everything, plus the Admin screen (users, roles, catalog)
  provost@…              Provost Office: everything, university-wide
  kok@…                  KOK: everything, university-wide
  admin.<school>@…       School administrator of that School
  head.<school>@…        Head of that School's Program
  karimova@…             Professor; with `instructor=True` co-teaches every course (the non-lead view)
The emails are those of the frontend's user switcher (lib/auth.ts DEMO_USERS).
"""

from dataclasses import dataclass, field

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import passwords
from app.config import get_settings
from app.enums import Role
from app.models import Course, Program, RoleAssignment, School, User

DOMAIN = "demo.narxoz.kz"

# key (used in test emails), slug, name
SCHOOLS = [
    ("economics", "economics-management", "Школа экономики и менеджмента"),
    ("humanities", "humanities", "Гуманитарная школа"),
    ("law", "law-public-administration", "Школа права и государственного управления"),
    ("digital", "digital-technologies", "Школа цифровых технологий"),
]


@dataclass
class Report:
    created: list[str] = field(default_factory=list)
    roles: dict[str, list[str]] = field(default_factory=dict)
    courses_taught: int = 0


async def setup_schools(session: AsyncSession) -> list[tuple[str, bool]]:
    """Create the four Schools and their Program. Returns (name, created now) per School."""
    result = []
    for _, slug, name in SCHOOLS:
        school = await session.scalar(sa.select(School).where(School.slug == slug))
        created = school is None
        if school is None:
            school = School(slug=slug, name=name)
            session.add(school)
            await session.flush()
        if not await session.scalar(sa.select(Program.id).where(Program.school_id == school.id).limit(1)):
            session.add(
                Program(slug=f"{slug}-programs", name=name, school_id=school.id, degree_level="Not set")
            )
        result.append((name, created))
    await session.flush()
    return result


async def _user(session: AsyncSession, email: str, name: str, title: str | None, report: Report) -> User:
    user = await session.scalar(sa.select(User).where(User.email == email))
    if user is None:
        user = User(email=email, name=name, title=title, roles=[])
        session.add(user)
        await session.flush()
        report.created.append(email)
    report.roles.setdefault(email, [])
    return user


def _grant(
    user: User,
    role: Role,
    report: Report,
    *,
    school: School | None = None,
    program: Program | None = None,
) -> None:
    school_id, program_id = (school.id if school else None), (program.id if program else None)
    if not any(
        r.role == role and r.school_id == school_id and r.program_id == program_id for r in user.roles
    ):
        user.roles.append(RoleAssignment(role=role, school_id=school_id, program_id=program_id))
    scope = school.name if school else program.name if program else ""
    report.roles[user.email].append(f"{role.value} ({scope})" if scope else role.value)


async def add_demo_users(
    session: AsyncSession,
    *,
    instructor: bool = False,
    domain: str = DOMAIN,
    password: str | None = None,
    allow_prod: bool = False,
) -> Report:
    """`password`: every test user gets it (password sign-in). In prod only with allow_prod and a password:
    test users there are a deliberate choice, and without a password nobody could sign in as them anyway."""
    if get_settings().env == "prod" and not (allow_prod and password):
        raise RuntimeError("demo users are created with ENV=prod only with --allow-prod and --password")
    report = Report()

    admin = await _user(session, f"superadmin@{domain}", "System Administrator", None, report)
    _grant(admin, Role.admin, report)
    provost = await _user(session, f"provost@{domain}", "Provost Office", None, report)
    _grant(provost, Role.provost, report)
    kok = await _user(session, f"kok@{domain}", "KOK Secretary", None, report)
    _grant(kok, Role.kok, report)

    for key, slug, name in SCHOOLS:
        school = await session.scalar(sa.select(School).where(School.slug == slug))
        if school is None:  # setup-schools was not run, or this database has other Schools
            continue
        admin = await _user(session, f"admin.{key}@{domain}", f"Administrator · {name}", None, report)
        _grant(admin, Role.school_admin, report, school=school)
        program = await session.scalar(
            sa.select(Program).where(Program.school_id == school.id).order_by(Program.name).limit(1)
        )
        if program is not None:
            head = await _user(session, f"head.{key}@{domain}", f"Head of Program · {name}", None, report)
            _grant(head, Role.program_head, report, program=program)

    professor = await _user(session, f"karimova@{domain}", "M. Karimova", "Professor", report)
    _grant(professor, Role.professor, report)
    if instructor:
        not_led = sa.or_(Course.lead_id.is_(None), Course.lead_id != professor.id)
        for course in await session.scalars(sa.select(Course).where(not_led)):
            if all(i.id != professor.id for i in course.instructors):
                course.instructors.append(professor)
                report.courses_taught += 1
    if password is not None:
        for email in report.roles:
            user = await session.scalar(sa.select(User).where(User.email == email))
            if user is not None:
                user.password_hash = passwords.hash_password(password)
    await session.flush()
    return report
