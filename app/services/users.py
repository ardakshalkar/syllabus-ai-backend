"""Real users and their roles (`python -m app.cli add-user / list-users / disable-user`).

Google sign-in only proves who someone is; a person can use the app once they exist here with a role:
  add-user daniar.aitanov@narxoz.kz --role provost
  add-user ivanov@narxoz.kz --role school_admin --school digital
  add-user petrova@narxoz.kz --role program_head --school law     (the School's Program)
  add-user sidorov@narxoz.kz --role professor
A School is given by its key (economics, humanities, law, digital), slug or full name. Running add-user again
adds another role; existing roles stay.
"""

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

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
) -> tuple[User, bool]:
    """Create the user if needed and give them the role. Returns (user, created)."""
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


async def disable_user(session: AsyncSession, email: str) -> User:
    """Sign-ins and requests of this user are refused from now on; their history stays."""
    user = await session.scalar(sa.select(User).where(sa.func.lower(User.email) == email.strip().lower()))
    if user is None:
        raise UserError(f"no user {email!r}")
    user.is_active = False
    return user
