"""`setup-schools` and `add-test-users`: the four Schools and a user for every role, safe to run twice."""

import sqlalchemy as sa

from app.db import pg
from app.enums import Role
from app.models import Course, Program, RoleAssignment, School, User
from app.services.demo_users import SCHOOLS, add_demo_users, setup_schools


async def test_schools_and_users_for_every_role(ref, client_for):
    async with pg.session_scope() as session:
        first_schools = await setup_schools(session)
        first = await add_demo_users(session, instructor=True, domain="t.test")
    async with pg.session_scope() as session:
        again_schools = await setup_schools(session)
        second = await add_demo_users(session, instructor=True, domain="t.test")
        names = set(await session.scalars(sa.select(School.name)))
        programs = await session.scalar(
            sa.select(sa.func.count())
            .select_from(Program)
            .join(School)
            .where(School.slug.in_([s[1] for s in SCHOOLS]))
        )
        roles = {
            (email, role)
            for email, role in (
                await session.execute(sa.select(User.email, RoleAssignment.role).join(RoleAssignment.user))
            ).all()
            if email.endswith("@t.test")
        }

    assert [created for _, created in first_schools] == [True] * 5
    assert [created for _, created in again_schools] == [False] * 5
    assert {name for _, _, name in SCHOOLS} <= names and programs == 5
    # superadmin, provost, kok, 5 admins, 5 heads, the professor
    assert len(first.created) == 14 and second.created == []
    assert ("superadmin@t.test", Role.admin) in roles
    assert ("admin.law@t.test", Role.school_admin) in roles and (
        "head.law@t.test",
        Role.program_head,
    ) in roles
    assert ("kok@t.test", Role.kok) in roles and ("provost@t.test", Role.provost) in roles
    assert first.courses_taught == len(ref["courses"]) and second.courses_taught == 0

    professor = client_for(type("U", (), {"email": "karimova@t.test"})())
    me = (await professor.get("/me")).json()
    async with pg.session_scope() as session:
        all_courses = {str(c) for c in await session.scalars(sa.select(Course.id))}
    assert set(me["taught_course_ids"]) == all_courses
