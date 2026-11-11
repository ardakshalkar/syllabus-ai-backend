"""The Admin screen's API (/admin/*, app/services/admin.py): only the `admin` role; users, roles, catalog, stats."""

from app.db import pg
from app.enums import Role
from app.services import users
from tests.conftest import create_syllabus

ROOT = "root@narxoz.kz"


def as_email(client_for, email: str):
    return client_for(type("U", (), {"email": email})())


async def _admin(client_for, email: str = ROOT):
    async with pg.session_scope() as session:
        await users.add_user(session, email, Role.admin, name="Root")
    return as_email(client_for, email)


async def test_only_admins_reach_the_admin_api(ref, client_for):
    root = await _admin(client_for)
    for name in ("provost", "kok", "admin.digital", "head.cs", "shalkar"):
        response = await client_for(ref["users"][name]).get("/admin/users")
        assert response.status_code == 403, name

    listed = await root.get("/admin/users")
    assert listed.status_code == 200, listed.text
    emails = {u["email"] for u in listed.json()}
    assert ROOT in emails and ref["users"]["shalkar"].email in emails
    only_admins = (await root.get("/admin/users", params={"role": "admin"})).json()
    assert [u["email"] for u in only_admins] == [ROOT]
    found = (await root.get("/admin/users", params={"q": "SHALKAR"})).json()
    assert [u["email"] for u in found] == [ref["users"]["shalkar"].email]

    # An admin is university-wide like the Provost Office: every syllabus, Schools, analytics.
    lead = client_for(ref["users"]["shalkar"])
    syllabus = await create_syllabus(lead, ref["courses"]["ai"].id)
    assert (await root.get(f"/syllabi/{syllabus['id']}")).status_code == 200
    assert (await root.post("/schools", json={"slug": "new-school", "name": "New"})).status_code == 201

    stats = (await root.get("/admin/stats")).json()
    assert stats["syllabi_total"] == 1 and stats["users_by_role"]["admin"] == 1
    assert stats["schools"] == len(ref["schools"]) + 1 and stats["courses"] == len(ref["courses"])


async def test_users_and_roles_are_managed(ref, client_for):
    root = await _admin(client_for)
    digital, cs = str(ref["schools"]["digital"].id), str(ref["programs"]["cs"].id)

    bad = await root.post("/admin/users", json={"email": "x@narxoz.kz", "roles": [{"role": "school_admin"}]})
    assert bad.status_code == 422
    created = await root.post(
        "/admin/users",
        json={
            "email": " New.Person@narxoz.kz",
            "name": "New Person",
            "password": "long enough password",
            "roles": [{"role": "school_admin", "school_id": digital}, {"role": "professor"}],
        },
    )
    assert created.status_code == 201, created.text
    user = created.json()
    assert user["email"] == "new.person@narxoz.kz" and user["has_password"] is True
    assert {(r["role"], r["scope"]) for r in user["roles"]} == {
        ("school_admin", ref["schools"]["digital"].name),
        ("professor", None),
    }
    again = await root.post("/admin/users", json={"email": "new.person@narxoz.kz"})
    assert again.status_code == 409

    uid = user["id"]
    person = as_email(client_for, "new.person@narxoz.kz")
    assert (await person.get("/me")).status_code == 200

    head = await root.post(f"/admin/users/{uid}/roles", json={"role": "program_head", "program_id": cs})
    assert head.status_code == 200 and len(head.json()["roles"]) == 3
    professor = next(r for r in head.json()["roles"] if r["role"] == "professor")
    removed = await root.delete(f"/admin/users/{uid}/roles/{professor['id']}")
    assert {r["role"] for r in removed.json()["roles"]} == {"school_admin", "program_head"}

    no_password = await root.delete(f"/admin/users/{uid}/password")
    assert no_password.json()["has_password"] is False
    short = await root.put(f"/admin/users/{uid}/password", json={"password": "short"})
    assert short.status_code == 422

    patched = await root.patch(
        f"/admin/users/{uid}",
        json={
            "name": "Renamed",
            "title": None,
            "department": "New Dept",
            "school_id": digital,
            "is_active": False,
        },
    )
    assert (
        patched.json()["name"] == "Renamed"
        and patched.json()["department"] == "New Dept"
        and patched.json()["school_id"] == digital
        and patched.json()["is_active"] is False
    )
    assert (await person.get("/me")).status_code == 403  # a disabled user's requests are refused


async def test_admin_employees_endpoint(ref, client_for):
    root = await _admin(client_for)
    res = await root.get("/admin/employees")
    assert res.status_code == 200
    employees = res.json()
    assert len(employees) > 0
    first = employees[0]
    assert "full_name" in first and "email" in first and "department" in first
    assert "already_added" in first and "school" in first
    # Verify sorting by school order
    schools_present = [e["school"] for e in employees if e.get("school")]
    assert schools_present[0] == "Школа экономики и менеджмента"
    assert "Школа цифровых технологий" in schools_present


async def test_an_admin_cannot_lock_themselves_out(ref, client_for):
    root = await _admin(client_for)
    me = (await root.get("/admin/users", params={"q": ROOT})).json()[0]
    assert (await root.patch(f"/admin/users/{me['id']}", json={"is_active": False})).status_code == 409
    own_role = me["roles"][0]["id"]
    assert (await root.delete(f"/admin/users/{me['id']}/roles/{own_role}")).status_code == 409

    # Another admin may take the role away.
    other = await _admin(client_for, "second@narxoz.kz")
    assert (await other.delete(f"/admin/users/{me['id']}/roles/{own_role}")).status_code == 200
    assert (await root.get("/admin/users")).status_code == 403


async def test_catalog_is_edited_and_deleted_safely(ref, client_for):
    root = await _admin(client_for)
    digital = ref["schools"]["digital"].id

    renamed = await root.patch(f"/admin/schools/{digital}", json={"name": "Digital School"})
    assert renamed.status_code == 200 and renamed.json()["name"] == "Digital School"
    assert renamed.json()["slug"] == ref["schools"]["digital"].slug
    assert (await root.delete(f"/admin/schools/{digital}")).status_code == 409  # it has programs

    empty = (await root.post("/schools", json={"slug": "empty", "name": "Empty"})).json()
    assert (await root.delete(f"/admin/schools/{empty['id']}")).status_code == 204

    ai = ref["courses"]["ai"]
    lee = str(ref["users"]["lee"].id)
    course = await root.patch(
        f"/admin/courses/{ai.id}", json={"credits": 6, "lead_id": lee, "instructor_ids": []}
    )
    assert course.status_code == 200, course.text
    assert course.json()["credits"] == 6 and course.json()["lead_id"] == lee
    assert course.json()["instructor_ids"] == [lee]  # the lead always teaches
    unled = await root.patch(f"/admin/courses/{ai.id}", json={"lead_id": None})
    assert unled.json()["lead_id"] is None and unled.json()["credits"] == 6

    program = await root.patch(f"/admin/programs/{ref['programs']['cs'].id}", json={"passport_text": "PLO1."})
    assert program.json()["has_passport"] is True

    await create_syllabus(client_for(ref["users"]["lee"]), ref["courses"]["ml"].id)
    assert (await root.delete(f"/admin/courses/{ref['courses']['ml'].id}")).status_code == 409
    assert (await root.delete(f"/admin/programs/{ref['programs']['cs'].id}")).status_code == 409


async def test_users_are_imported_from_csv_all_or_nothing(ref, client_for):
    """`import-users` / scripts/import_users.sh: one line per role, idempotent, nothing saved when a line is wrong."""
    import sqlalchemy as sa

    from app.models import User

    csv_text = """email,role,school,program,name,title
# a comment line
it.admin@narxoz.kz,admin,,,IT Admin,
 Head.CS@narxoz.kz , program_head ,,cs,Head CS,Head of Program
both@narxoz.kz,school_admin,digital,,Both Roles,
both@narxoz.kz,professor,,,,
"""
    async with pg.session_scope() as session:
        first = await users.import_users(session, csv_text, password="long enough password")
    assert first.errors == []
    assert first.created == ["it.admin@narxoz.kz", "head.cs@narxoz.kz", "both@narxoz.kz"]
    async with pg.session_scope() as session:
        again = await users.import_users(session, csv_text)
        both = await session.scalar(sa.select(User).where(User.email == "both@narxoz.kz"))
        assert {r.role for r in both.roles} == {Role.school_admin, Role.professor}
        assert both.password_hash is not None  # the second run without a password keeps it
    assert again.errors == [] and again.created == [] and len(again.updated) == 3
    assert (await as_email(client_for, "it.admin@narxoz.kz").get("/admin/stats")).status_code == 200

    bad = "email,role,school\nok@narxoz.kz,professor,\nx@narxoz.kz,dean,\ny@narxoz.kz,school_admin,\n"
    async with pg.session_scope() as session:
        report = await users.import_users(session, bad)
        assert len(report.errors) == 2 and "line 3" in report.errors[0] and "line 4" in report.errors[1]
        await session.rollback()
    async with pg.session_scope() as session:
        assert await session.scalar(sa.select(User.id).where(User.email == "ok@narxoz.kz")) is None
        header = await users.import_users(session, "mail,role\na@b.c,professor\n")
    assert header.errors and "header" in header.errors[0]
