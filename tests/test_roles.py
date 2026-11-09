"""Rights of the three managing roles: Provost Office and KOK everywhere, a School administrator in their School."""

from seed.samples import sample_syllabus
from tests.conftest import create_syllabus, upload


async def _uploaded(ref, client_for, course: str, lead: str) -> dict:
    client = client_for(ref["users"][lead])
    syllabus = await create_syllabus(client, ref["courses"][course].id)
    return (await upload(client, syllabus["id"], sample_syllabus(course, with_issues=False)))["version"]


async def test_who_can_start_a_check(ref, client_for):
    digital = await _uploaded(ref, client_for, "ai", "shalkar")  # School of Digital Technologies
    economics = await _uploaded(ref, client_for, "corp", "karimova")  # School of Economics

    admin = client_for(ref["users"]["admin.digital"])
    assert (await admin.post(f"/versions/{digital['id']}/runs")).status_code == 201
    assert (await admin.post(f"/versions/{economics['id']}/runs")).status_code == 404  # not their School
    for role in ("kok", "provost"):
        client = client_for(ref["users"][role])
        assert (await client.post(f"/versions/{economics['id']}/runs")).status_code == 201
    teacher = client_for(ref["users"]["lee"])  # teaches AI but does not lead it
    assert (await teacher.post(f"/versions/{digital['id']}/runs")).status_code == 403


async def test_kok_runs_sessions_like_the_provost_office(ref, client_for):
    kok = client_for(ref["users"]["kok"])
    payload = {"name": "KOK review", "scope_type": "university", "semester": "Fall 2026"}
    response = await kok.post("/sessions", json=payload)
    assert response.status_code == 201, response.text
    school = await kok.post(
        "/sessions",
        json={**payload, "scope_type": "school", "scope_ref": str(ref["schools"]["economics"].id)},
    )
    assert school.status_code == 201
