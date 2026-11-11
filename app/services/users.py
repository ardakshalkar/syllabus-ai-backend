"""Real users and their roles (`python -m app.cli add-user / list-users / disable-user`).

Google sign-in only proves who someone is; a person can use the app once they exist here with a role:
  add-user daniar.aitanov@narxoz.kz --role provost
  add-user ivanov@narxoz.kz --role school_admin --school digital
  add-user petrova@narxoz.kz --role program_head --school law     (the School's Program)
  add-user sidorov@narxoz.kz --role professor
A School is given by its key (economics, humanities, law, digital), slug or full name. Running add-user again
adds another role; existing roles stay.

Many at once: `python -m app.cli import-users users.csv` (or scripts/import_users.sh on a server), see import_users.
"""

import csv
import io
from dataclasses import dataclass, field

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import passwords
from app.enums import Role
from app.models import Program, RoleAssignment, School, User
from app.services.demo_users import SCHOOLS


class UserError(ValueError):
    pass


async def find_school(session: AsyncSession, ref: str) -> School:
    keys = {key: slug for key, slug, _ in SCHOOLS}
    ref = ref.strip()
    slug = keys.get(ref.lower(), ref)
    school = await session.scalar(
        sa.select(School)
        .where(
            sa.or_(
                School.slug.in_([slug, ref.lower()]),
                sa.func.lower(School.name) == ref.lower(),
            )
        )
        .limit(1)
    )
    if school is None:
        raise UserError(f"no School {ref!r}: use one of {', '.join(keys)} (run setup-schools first)")
    return school


async def add_user(
    session: AsyncSession,
    email: str,
    role: Role,
    *,
    name: str | None = None,
    title: str | None = None,
    school: str | None = None,
    program: str | None = None,
    password: str | None = None,
) -> tuple[User, bool]:
    """Create the user if needed and give them the role (and a password, if given). Returns (user, created)."""
    email = email.strip().lower()
    if "@" not in email:
        raise UserError(f"not an email: {email!r}")
    school_id = program_id = None
    if role == Role.program_head and program:
        program_id = await session.scalar(sa.select(Program.id).where(Program.slug == program.strip()))
        if program_id is None:
            raise UserError(f"no program with slug {program!r}")
    elif program:
        raise UserError("--program is only for program_head")
    elif role in (Role.school_admin, Role.program_head):
        if not school:
            raise UserError(f"--school is required for {role.value}")
        found = await find_school(session, school)
        if role == Role.school_admin:
            school_id = found.id
        else:
            program_id = await session.scalar(
                sa.select(Program.id).where(Program.school_id == found.id).order_by(Program.name).limit(1)
            )
            if program_id is None:
                raise UserError(f"{found.name} has no Program (run setup-schools)")
    elif school:
        raise UserError(f"{role.value} is not tied to a School: drop --school")

    user = await session.scalar(sa.select(User).where(sa.func.lower(User.email) == email))
    created = user is None
    if user is None:
        user = User(email=email, name=name or email, title=title, roles=[])
        session.add(user)
        await session.flush()
    else:
        user.name = name or user.name
        user.title = title or user.title
    user.is_active = True
    if password is not None:
        user.password_hash = _hash(password)
    if not any(
        r.role == role and r.school_id == school_id and r.program_id == program_id for r in user.roles
    ):
        user.roles.append(RoleAssignment(role=role, school_id=school_id, program_id=program_id))
    await session.flush()
    return user, created


async def list_users(session: AsyncSession) -> list[tuple[User, list[str]]]:
    schools = {s.id: s.name for s in await session.scalars(sa.select(School))}
    programs = {p.id: p.name for p in await session.scalars(sa.select(Program))}
    result = []
    for user in await session.scalars(sa.select(User).order_by(User.email)):
        roles = []
        for r in user.roles:
            scope = (schools.get(r.school_id) if r.school_id else None) or (
                programs.get(r.program_id) if r.program_id else None
            )
            roles.append(f"{r.role.value} ({scope})" if scope else r.role.value)
        result.append((user, roles))
    return result


def _hash(password: str) -> str:
    try:
        return passwords.hash_password(password)
    except ValueError as exc:
        raise UserError(str(exc)) from exc


async def set_password(session: AsyncSession, email: str, password: str | None) -> User:
    """Set (or with None remove) the user's password. Their other password sessions end."""
    user = await session.scalar(sa.select(User).where(sa.func.lower(User.email) == email.strip().lower()))
    if user is None:
        raise UserError(f"no user {email!r}: add-user first")
    user.password_hash = _hash(password) if password is not None else None
    return user


async def disable_user(session: AsyncSession, email: str) -> User:
    """Sign-ins and requests of this user are refused from now on; their history stays."""
    user = await session.scalar(sa.select(User).where(sa.func.lower(User.email) == email.strip().lower()))
    if user is None:
        raise UserError(f"no user {email!r}")
    user.is_active = False
    return user


IMPORT_COLUMNS = ("email", "role", "school", "program", "name", "title")


@dataclass
class ImportReport:
    created: list[str] = field(default_factory=list)
    updated: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)


async def import_users(session: AsyncSession, text: str, password: str | None = None) -> ImportReport:
    """Users from CSV text with the header `email,role,school,program,name,title` (only email and role are
    required; `#` lines and blank lines are skipped). A person with several roles takes one line per role.
    Each line is add_user, so importing the same file again only adds what is missing. `password`: given
    to every imported user (email + password sign-in); without it they sign in with Google.
    All or nothing: when any line is wrong, `errors` lists them and the caller must roll back."""
    report = ImportReport()
    lines = [line for line in text.splitlines() if line.strip() and not line.lstrip().startswith("#")]
    rows = list(csv.DictReader(io.StringIO("\n".join(lines)), skipinitialspace=True))
    if not lines or "email" not in (rows[0].keys() if rows else lines[0].split(",")):
        report.errors.append(f"the first line must be the header: {','.join(IMPORT_COLUMNS)}")
        return report
    if unknown := set(rows[0].keys() if rows else []) - set(IMPORT_COLUMNS):
        report.errors.append(
            f"unknown column(s) {', '.join(sorted(map(str, unknown)))}: use {','.join(IMPORT_COLUMNS)}"
        )
        return report
    for number, row in enumerate(rows, start=2):
        value = {k: (row.get(k) or "").strip() or None for k in IMPORT_COLUMNS}
        try:
            role = Role((value["role"] or "").lower())
        except ValueError:
            report.errors.append(f"line {number}: role must be one of {', '.join(r.value for r in Role)}")
            continue
        try:
            user, created = await add_user(
                session,
                value["email"] or "",
                role,
                name=value["name"],
                title=value["title"],
                school=value["school"],
                program=value["program"],
                password=password,
            )
        except UserError as exc:
            report.errors.append(f"line {number} ({value['email']}): {exc}")
            continue
        if created:
            report.created.append(user.email)
        elif user.email not in report.created and user.email not in report.updated:
            report.updated.append(user.email)
    return report
