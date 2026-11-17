"""How the AI checks syllabi (services.check_mode): the administrator's choice, kept by each check session,
shown with its price in the estimate, and used when the check runs."""

from seed.samples import sample_syllabus
from tests.conftest import create_syllabus, upload
from tests.test_admin import _admin

QWEN = {"mode": "single", "model": "qwen/qwen4-max"}


async def test_only_the_administrator_chooses_how_the_ai_checks(ref, client_for):
    root = await _admin(client_for)
    first = (await root.get("/admin/check-mode")).json()
    assert first["mode"] == "standard" and first["updated_at"] is None
    assert [(o["mode"], o["model"]) for o in first["options"]] == [
        ("standard", None),
        ("single", "google/gemma-4-31b-it"),
        ("single", "qwen/qwen4-max"),
    ]

    for name in ("provost", "kok", "admin.digital"):
        assert (await client_for(ref["users"][name]).put("/admin/check-mode", json=QWEN)).status_code == 403
    bad = await root.put("/admin/check-mode", json={"mode": "single", "model": "some/other-model"})
    assert bad.status_code == 422

    saved = await root.put("/admin/check-mode", json=QWEN)
    assert saved.status_code == 200, saved.text
    assert saved.json()["mode"] == "single" and saved.json()["updated_by_name"] == "Root"
    # Stored, not only answered: a new request reads it back.
    again = (await root.get("/admin/check-mode")).json()
    assert (again["mode"], again["model"]) == ("single", "qwen/qwen4-max")


async def test_a_check_runs_the_way_it_was_estimated(ref, client_for):
    root = await _admin(client_for)
    await root.put("/admin/check-mode", json=QWEN)
    lead = client_for(ref["users"]["shalkar"])
    syllabus = await create_syllabus(lead, ref["courses"]["ai"].id)
    await upload(lead, syllabus["id"], sample_syllabus("ai", with_issues=False))

    admin = client_for(ref["users"]["admin.digital"])
    body = {"scope_type": "school", "scope_ref": str(ref["schools"]["digital"].id), "semester": "Fall 2026"}
    estimate = (await admin.post("/sessions/estimate", json=body)).json()
    assert (estimate["check_mode"], estimate["check_model"]) == ("single", "qwen/qwen4-max")

    created = await admin.post("/sessions", json={**body, "name": "Digital review"})
    assert created.status_code == 201, created.text
    session = created.json()
    assert (session["check_mode"], session["check_model"]) == ("single", "qwen/qwen4-max")

    # Changing the setting later does not change a session already created.
    await root.put("/admin/check-mode", json={"mode": "standard"})
    kept = (await admin.get(f"/sessions/{session['id']}")).json()
    assert (kept["check_mode"], kept["check_model"]) == ("single", "qwen/qwen4-max")
