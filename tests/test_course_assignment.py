"""Setting the programs of many courses at once: the School's list, a bulk change and a CSV."""

from app.db import pg
from app.models import Program, ProgramOutcome


async def _programs(ref, *codes: str) -> dict[str, str]:
    ids = {}
    async with pg.session_scope() as session:
        for code in codes:
            program = Program(
                slug=code.lower(),
                code=code,
                name=f"Program {code}",
                school_id=ref["schools"]["digital"].id,
                degree_level="Bachelor",
                titles={"en": f"Program {code}"},
                outcomes=[ProgramOutcome(number=1, text={"en": "Does things."})],
                disciplines=[],
            )
            session.add(program)
            await session.flush()
            ids[code] = str(program.id)
    return ids


async def test_the_school_list_and_a_bulk_change(ref, client_for):
    ids = await _programs(ref, "6B01001", "6B01002")
    school = ref["schools"]["digital"].id
    admin = client_for(ref["users"]["admin.digital"])
    ai = ref["courses"]["ai"]

    listing = (await admin.get(f"/schools/{school}/course-programs")).json()
    assert listing["total"] == sum(listing["counts"].values()) and listing["counts"].get("none")
    assert all(r["state"] == "none" for r in listing["items"])  # nothing matched in the seed
    assert (await admin.get(f"/schools/{school}/course-programs", params={"q": ai.name})).json()["total"] >= 1

    done = await admin.post(
        f"/schools/{school}/course-programs",
        json={"items": [{"course_id": str(ai.id), "program_ids": [ids["6B01001"], ids["6B01002"]]}]},
    )
    assert done.status_code == 200, done.text
    assert done.json() == {"set": 1, "cleared": 0}
    row = next(
        r
        for r in (await admin.get(f"/schools/{school}/course-programs", params={"state": "manual"})).json()[
            "items"
        ]
        if r["course_id"] == str(ai.id)
    )
    assert {p["code"] for p in row["programs"]} == {"6B01001", "6B01002"}

    # All or nothing: one bad item changes nothing.
    bad = await admin.post(
        f"/schools/{school}/course-programs",
        json={
            "items": [
                {"course_id": str(ai.id), "program_ids": []},
                {"course_id": str(ai.id), "program_ids": []},
            ]
        },
    )
    assert bad.status_code == 422  # listed twice
    unknown = await admin.post(
        f"/schools/{school}/course-programs",
        json={"items": [{"course_id": str(ai.id), "program_ids": [str(ref["programs"]["cs"].id)]}]},
    )
    assert unknown.status_code == 422  # a placeholder is not an imported program
    still = (await admin.get(f"/schools/{school}/course-programs", params={"state": "manual"})).json()
    assert still["total"] == 1

    cleared = await admin.post(
        f"/schools/{school}/course-programs", json={"items": [{"course_id": str(ai.id), "program_ids": []}]}
    )
    assert cleared.json() == {"set": 0, "cleared": 1}


async def test_only_the_schools_managers_use_it(ref, client_for):
    economics = ref["schools"]["economics"].id
    assert (
        await client_for(ref["users"]["admin.digital"]).get(f"/schools/{economics}/course-programs")
    ).status_code == 403
    assert (
        await client_for(ref["users"]["kok"]).get(f"/schools/{economics}/course-programs")
    ).status_code == 200
    foreign = await client_for(ref["users"]["kok"]).post(
        f"/schools/{ref['schools']['digital'].id}/course-programs",
        json={"items": [{"course_id": str(ref["courses"]["intro"].id), "program_ids": []}]},
    )
    assert foreign.status_code == 404  # an economics course is not a digital one


async def test_csv_is_checked_first_and_only_the_ready_rows_are_applied(ref, client_for):
    ids = await _programs(ref, "6B01001", "6B01002")
    school = ref["schools"]["digital"].id
    admin = client_for(ref["users"]["admin.digital"])
    ai = ref["courses"]["ai"]
    text = (
        "course,program\n"
        f"{ai.name},6В01001; 6B01002\n"  # Cyrillic В is accepted
        "No such course,6B01001\n"
        f"{ai.name},6B09999\n"  # a second row for a course already set, and an unknown program
    )
    preview = await admin.post(f"/schools/{school}/course-programs/csv", json={"csv": text})
    assert preview.status_code == 200, preview.text
    body = preview.json()
    assert (body["ready"], body["problems"]) == (1, 2)
    assert body["rows"][1]["problems"] == ["course not found in this School"]
    assert any("unknown program" in p for p in body["rows"][2]["problems"])
    listing = (await admin.get(f"/schools/{school}/course-programs", params={"state": "manual"})).json()
    assert listing["total"] == 0  # the preview changed nothing

    applied = await admin.put(f"/schools/{school}/course-programs/csv", json={"csv": text})
    assert applied.status_code == 200, applied.text
    assert (applied.json()["set"], applied.json()["skipped"]) == (1, 2)
    row = (await admin.get(f"/schools/{school}/course-programs", params={"state": "manual"})).json()["items"][
        0
    ]
    assert {p["code"] for p in row["programs"]} == {"6B01001", "6B01002"}
    assert ids  # both programs exist

    no_header = await admin.post(f"/schools/{school}/course-programs/csv", json={"csv": "a,b\n1,2\n"})
    assert no_header.status_code == 422
