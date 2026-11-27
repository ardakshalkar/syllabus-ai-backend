"""Programs, their outcomes and their course lists edited by hand (PATCH/DELETE /programs, /programs/{id}/disciplines),
the way an import keeps those edits, and the change log (GET /programs/{id}/events)."""

import sqlalchemy as sa

from app.db import pg
from app.models import CatalogEvent, Program, School
from app.services import program_import as pi
from tests.test_program_import import site_program


def _body(client_school, **extra) -> dict:
    return {
        "slug": "law-new",
        "name": "Law (new)",
        "school_id": str(client_school),
        "degree_level": "Bachelor",
        **extra,
    }


async def test_a_program_is_made_by_hand_with_outcomes_and_courses(ref, client_for):
    kok = client_for(ref["users"]["kok"])
    kok.headers["X-Client"] = "mcp"
    school = ref["schools"]["digital"].id

    # No code: a placeholder, which has no outcomes to edit.
    placeholder = await kok.post("/programs", json=_body(school, slug="plain"))
    assert placeholder.status_code == 201, placeholder.text
    assert (
        await kok.post(f"/programs/{placeholder.json()['id']}/outcomes", json={"text": {"en": "x"}})
    ).status_code == 404

    created = await kok.post(
        "/programs", json=_body(school, code="6В04299")
    )  # Cyrillic В is turned into Latin
    assert created.status_code == 201, created.text
    program_id = created.json()["id"]
    assert created.json()["code"] == "6B04299"
    assert (await kok.post("/programs", json=_body(school, slug="dup", code="6B04299"))).status_code == 409
    assert (await kok.post("/programs", json=_body(school, slug="bad", code="nope"))).status_code == 422

    outcome = await kok.post(f"/programs/{program_id}/outcomes", json={"text": {"ru": "Знает право"}})
    assert outcome.status_code == 201, outcome.text

    added = await kok.post(
        f"/programs/{program_id}/disciplines", json={"name": {"ru": "Гражданское право", "en": "Civil law"}}
    )
    assert added.status_code == 201, added.text
    assert added.json()["disciplines"][0]["source"] == "manual"
    renamed = await kok.patch(
        f"/programs/{program_id}/disciplines/1", json={"name": {"en": "Civil law I"}, "is_profile": True}
    )
    assert renamed.json()["disciplines"][0]["name"] == {"en": "Civil law I"}
    assert (
        await kok.post(f"/programs/{program_id}/disciplines", json={"name": {"de": "x"}})
    ).status_code == 422
    assert (await kok.post(f"/programs/{program_id}/disciplines", json={"name": {}})).status_code == 422
    two = await kok.post(f"/programs/{program_id}/disciplines", json={"name": {"en": "Tax law"}})
    assert [d["number"] for d in two.json()["disciplines"]] == [1, 2]
    gone = await kok.delete(f"/programs/{program_id}/disciplines/1")
    assert [(d["number"], d["name"]["en"]) for d in gone.json()["disciplines"]] == [(1, "Tax law")]

    edited = await kok.patch(
        f"/programs/{program_id}", json={"name": "Law, renamed", "passport_text": "PLO1"}
    )
    assert edited.json()["name"] == "Law, renamed" and edited.json()["has_passport"] is True

    events = (await kok.get(f"/programs/{program_id}/events")).json()
    assert {(e["entity"], e["action"]) for e in events} == {
        ("program", "create"),
        ("outcome", "create"),
        ("course", "create"),
        ("course", "edit"),
        ("course", "delete"),
        ("program", "edit"),
    }
    assert {e["channel"] for e in events} == {"mcp"}  # every call named its client


async def test_only_the_programs_own_administrators_may_edit_it(ref, client_for):
    async with pg.session_scope() as session:
        economics = School(slug="economics-management", name="Economics")
        session.add(economics)
        await session.flush()
        await pi.import_programs(session, [site_program()])
        program_id = await session.scalar(sa.select(Program.id).where(Program.code == "6B04103"))

    other = client_for(ref["users"]["admin.digital"])  # administers a different School
    assert (await other.patch(f"/programs/{program_id}", json={"name": "x"})).status_code == 403
    assert (
        await other.post(f"/programs/{program_id}/disciplines", json={"name": {"en": "x"}})
    ).status_code == 403
    assert (await other.delete(f"/programs/{program_id}")).status_code == 403
    assert (await other.get(f"/programs/{program_id}/events")).status_code == 403

    kok = client_for(ref["users"]["kok"])
    assert (await kok.patch(f"/programs/{program_id}", json={"name": "Finance (ours)"})).status_code == 200
    assert (await kok.patch(f"/programs/{program_id}", json={"name": ""})).status_code == 422


async def test_an_import_keeps_what_was_edited_or_added_by_hand(ref, client_for):
    async with pg.session_scope() as session:
        session.add(School(slug="economics-management", name="Economics"))
        await session.flush()
        await pi.import_programs(session, [site_program()])
        program_id = await session.scalar(sa.select(Program.id).where(Program.code == "6B04103"))

    kok = client_for(ref["users"]["kok"])
    # The site lists Statistics and Corporate finance. Rename the first, add a third by hand.
    await kok.patch(f"/programs/{program_id}/disciplines/1", json={"name": {"en": "Statistics (ours)"}})
    await kok.post(f"/programs/{program_id}/disciplines", json={"name": {"en": "Our elective"}})
    await kok.patch(f"/programs/{program_id}", json={"name": "Finance (ours)", "degree_level": "Bachelor+"})
    # A site course cannot be deleted (it would come back); a manual one can.
    assert (await kok.delete(f"/programs/{program_id}/disciplines/1")).status_code == 409

    # The site now lists three courses, the second one reworded.
    changed = {lang: detail_with_three(lang) for lang in pi.LANGS}
    async with pg.session_scope() as session:
        await pi.import_programs(session, [pi.parse_program(changed)])

    got = (await kok.get(f"/programs/{program_id}")).json()
    assert got["name"] == "Finance (ours)" and got["degree_level"] == "Bachelor+"
    rows = [(d["number"], d["name"]["en"], d["source"], d["edited"]) for d in got["disciplines"]]
    assert rows == [
        (1, "Statistics (ours)", "site", True),  # edited: kept
        (2, "Corporate finance NEW", "site", False),  # refreshed from the site
        (3, "Auditing en", "site", False),  # new on the site
        (4, "Our elective", "manual", True),  # added by hand: after the site's
    ]


def detail_with_three(lang: str) -> dict:
    from tests.test_program_import import detail

    d = detail(lang)
    d["disciplines"] = [
        {"id": 1, "sort_order": 1, "is_profile_subject": False, "name": f"Statistics {lang}"},
        {
            "id": 2,
            "sort_order": 2,
            "is_profile_subject": True,
            "name": f"Corporate finance {'NEW' if lang == 'en' else lang}",
        },
        {"id": 4, "sort_order": 3, "is_profile_subject": False, "name": f"Auditing {lang}"},
    ]
    return d


async def test_a_program_with_courses_filed_under_it_is_not_deleted_and_its_log_stays(ref, client_for):
    kok = client_for(ref["users"]["kok"])
    cs = ref["programs"]["cs"]  # the demo program the seed courses are filed under
    refused = await kok.delete(f"/programs/{cs.id}")
    assert refused.status_code == 409, refused.text

    created = await kok.post("/programs", json=_body(ref["schools"]["digital"].id))
    program_id = created.json()["id"]
    assert (await kok.delete(f"/programs/{program_id}")).status_code == 204
    assert (await kok.get(f"/programs/{program_id}")).status_code == 404
    async with pg.session_scope() as session:
        actions = (
            await session.scalars(
                sa.select(CatalogEvent.action).where(CatalogEvent.program_id == created.json()["id"])
            )
        ).all()
    assert sorted(actions) == ["create", "delete"]
    log = await kok.get(f"/programs/{program_id}/events")
    assert log.status_code == 200 and len(log.json()) == 2
