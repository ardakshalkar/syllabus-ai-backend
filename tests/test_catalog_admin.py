"""Building the catalog through the API (PR #1: POST /schools, /programs, /courses) with the current roles,
and the Settings screen's /integrations."""

import sqlalchemy as sa

from app.db import pg
from app.enums import Role
from app.models import User
from app.services import users


async def test_schools_programs_and_courses_can_be_added(ref, client_for):
    kok = client_for(ref["users"]["kok"])
    school = await kok.post("/schools", json={"slug": "test-school", "name": "Test School"})
    assert school.status_code == 201, school.text  # KOK has the Provost Office's rights
    assert (await kok.post("/schools", json={"slug": "test-school", "name": "Again"})).status_code == 409
    assert (await kok.post("/schools", json={"slug": "Bad Slug", "name": "x"})).status_code == 422

    admin = client_for(ref["users"]["admin.digital"])
    digital = str(ref["schools"]["digital"].id)
    program = await admin.post(
        "/programs",
        json={
            "slug": "data-science",
            "name": "Data Science",
            "school_id": digital,
            "degree_level": "Bachelor",
            "passport_text": "PLO1. Analyse data.",
        },
    )
    assert program.status_code == 201, program.text
    assert program.json()["has_passport"] is True
    other = await admin.post(
        "/programs",
        json={"slug": "x-prog", "name": "X", "school_id": school.json()["id"], "degree_level": "Bachelor"},
    )
    assert other.status_code == 403  # not their School

    lead = str(ref["users"]["lee"].id)
    course = await admin.post(
        "/courses",
        json={
            "slug": "ds-101",
            "name": "Intro to DS",
            "program_id": program.json()["id"],
            "credits": 5,
            "lead_id": lead,
        },
    )
    assert course.status_code == 201, course.text
    assert course.json()["instructor_ids"] == [lead]
    professor = client_for(ref["users"]["shalkar"])
    assert (await professor.post("/schools", json={"slug": "p", "name": "P"})).status_code == 403
    listed = (await admin.get("/programs")).json()
    assert any(p["slug"] == "data-science" and p["has_passport"] for p in listed)


async def test_integrations_reports_what_the_app_uses(ref, client_for):
    body = (await client_for(ref["users"]["provost"]).get("/integrations")).json()
    assert body == {"auth": "stub", "drive": "stub", "email": "console", "llm": "fake"}


async def test_add_user_accepts_the_title_and_a_program_slug(ref):
    async with pg.session_scope() as session:
        await users.add_user(
            session, "head.cs2@narxoz.kz", Role.program_head, name="Head CS", title="Dr", program="cs"
        )
        user = await session.scalar(sa.select(User).where(User.email == "head.cs2@narxoz.kz"))
        assert (user.name, user.title) == ("Head CS", "Dr")
        assert user.roles[0].program_id == ref["programs"]["cs"].id
