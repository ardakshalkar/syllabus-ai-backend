"""Semesters (/semesters): everyone reads the list; the Provost Office and the system administrator keep it."""


async def test_everyone_reads_semesters_but_only_the_provost_office_adds_them(ref, client_for):
    provost = client_for(ref["users"]["provost"])
    for name in ("kok", "admin.digital", "head.cs", "shalkar"):
        client = client_for(ref["users"][name])
        assert (await client.get("/semesters")).status_code == 200, name
        refused = await client.post("/semesters", json={"name": "Spring 2027"})
        assert refused.status_code == 403, name

    created = await provost.post(
        "/semesters", json={"name": "Spring 2027", "starts_on": "2027-01-18", "ends_on": "2027-05-30"}
    )
    assert created.status_code == 201
    assert created.json()["is_current"] is False and created.json()["in_use"] == 0
    duplicate = await provost.post("/semesters", json={"name": "spring 2027"})
    assert duplicate.status_code == 409
    backwards = await provost.post(
        "/semesters", json={"name": "Fall 2027", "starts_on": "2027-12-01", "ends_on": "2027-09-01"}
    )
    assert backwards.status_code == 422


async def test_one_semester_is_current(ref, client_for):
    provost = client_for(ref["users"]["provost"])
    new = (await provost.post("/semesters", json={"name": "Spring 2027"})).json()
    made = await provost.patch(f"/semesters/{new['id']}", json={"is_current": True})
    assert made.status_code == 200 and made.json()["is_current"] is True
    current = [s["name"] for s in (await provost.get("/semesters")).json() if s["is_current"]]
    assert current == ["Spring 2027"]
    unset = await provost.patch(f"/semesters/{new['id']}", json={"is_current": False})
    assert unset.status_code == 422


async def test_only_an_unused_semester_can_be_deleted(ref, client_for):
    provost = client_for(ref["users"]["provost"])
    current = next(s for s in (await provost.get("/semesters")).json() if s["is_current"])
    assert (await provost.delete(f"/semesters/{current['id']}")).status_code == 409
    # A semester something already names: a folder registered for it.
    used = (await provost.post("/semesters", json={"name": "Autumn 2027"})).json()
    school = (await provost.get("/schools")).json()[0]
    folder = {"path": "Autumn 2027", "semester": "Autumn 2027"}
    assert (await provost.post(f"/schools/{school['id']}/folders", json=folder)).status_code == 201
    assert (await provost.delete(f"/semesters/{used['id']}")).status_code == 409
    spare = (await provost.post("/semesters", json={"name": "Summer 2027"})).json()
    assert (await provost.delete(f"/semesters/{spare['id']}")).status_code == 204
    assert "Summer 2027" not in {s["name"] for s in (await provost.get("/semesters")).json()}
