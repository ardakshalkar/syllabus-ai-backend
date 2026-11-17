"""A check of a file that has not changed takes the earlier result instead of asking the AI again
(services.checks.reusable_run); an administrator can make the AI run anyway (`force`)."""

import uuid

import sqlalchemy as sa

from app.db import pg
from app.documents import AssessmentResultDoc
from app.enums import Role, RunStatus
from app.models import AssessmentRun, SyllabusVersion
from app.services import checks, drive_catalog, users
from seed.samples import sample_syllabus
from tests.conftest import create_syllabus, upload
from tests.test_drive_catalog import _drive_syllabi, _link, _school_folder, _touch


def _count_ai_calls(monkeypatch) -> list[int]:
    calls: list[int] = []
    evaluate = checks.pipeline.evaluate

    async def counted(*args, **kwargs):
        calls.append(1)
        return await evaluate(*args, **kwargs)

    monkeypatch.setattr(checks.pipeline, "evaluate", counted)
    return calls


async def _root(client_for):
    async with pg.session_scope() as session:
        await users.add_user(session, "root@narxoz.kz", Role.admin, name="Root")
    return client_for(type("U", (), {"email": "root@narxoz.kz"})())


FILE = sample_syllabus("ai", with_issues=True)  # one file: a .docx saved again has new timestamps


async def _uploaded(ref, client_for) -> tuple[dict, dict]:
    lead = client_for(ref["users"]["shalkar"])
    syllabus = await create_syllabus(lead, ref["courses"]["ai"].id)
    uploaded = await upload(lead, syllabus["id"], FILE)
    return syllabus, uploaded["version"]


async def _results(run_id: str) -> list:
    doc = await AssessmentResultDoc.find_one(AssessmentResultDoc.run_id == uuid.UUID(run_id))
    assert doc is not None
    return [(r.criterion_id, r.status) for r in doc.results]


async def test_checking_an_unchanged_file_again_takes_the_earlier_result(ref, client_for, monkeypatch):
    syllabus, version = await _uploaded(ref, client_for)  # the upload is checked at once
    lead = client_for(ref["users"]["shalkar"])
    first = (await lead.get(f"/versions/{version['id']}/runs")).json()[0]
    calls = _count_ai_calls(monkeypatch)

    again = await lead.post(f"/versions/{version['id']}/runs")

    assert again.status_code == 201, again.text
    run = again.json()
    assert calls == []  # the AI was not asked
    assert run["status"] == RunStatus.completed and run["reused_from_id"] == first["id"]
    assert await _results(run["id"]) == await _results(first["id"])
    detail = (await lead.get(f"/syllabi/{syllabus['id']}")).json()
    assert detail["issues"] > 0  # the counts are the earlier check's


async def test_a_new_upload_of_the_same_file_takes_the_earlier_result(ref, client_for, monkeypatch):
    syllabus, version = await _uploaded(ref, client_for)
    lead = client_for(ref["users"]["shalkar"])
    calls = _count_ai_calls(monkeypatch)

    same = await upload(lead, syllabus["id"], FILE)
    changed = await upload(lead, syllabus["id"], sample_syllabus("ai", with_issues=False))

    reused = (await lead.get(f"/versions/{same['version']['id']}/runs")).json()[0]
    first = (await lead.get(f"/versions/{version['id']}/runs")).json()[0]
    assert reused["reused_from_id"] == first["id"]
    assert (await lead.get(f"/versions/{changed['version']['id']}/runs")).json()[0]["reused_from_id"] is None
    assert len(calls) == 1  # only the changed file went to the AI
    review = await lead.get(f"/versions/{same['version']['id']}/runs/latest")
    assert review.status_code == 200 and review.json()["results"]


async def test_a_new_drive_revision_with_the_same_bytes_is_not_sent_to_the_ai(ref, client_for, monkeypatch):
    root = _school_folder()
    await drive_catalog.sync_folder(await _link(ref, root))
    syllabus, _ = (await _drive_syllabi())[0]
    admin = client_for(ref["users"]["admin.digital"])
    version = (await admin.get(f"/syllabi/{syllabus.id}/versions")).json()[0]
    assert (await admin.post(f"/versions/{version['id']}/runs")).status_code == 201
    from app.config import get_settings

    path = get_settings().drive_stub_root / root / "Алгоритмизация и программирование" / "Силлабус RU.docx"
    _touch(path, 120)  # saved again in Drive, nothing changed
    calls = _count_ai_calls(monkeypatch)

    # The check follows the file to its newest revision in Drive (drive_catalog.follow_drive), finds the same bytes.
    run = (await admin.post(f"/versions/{version['id']}/runs")).json()

    versions = (await admin.get(f"/syllabi/{syllabus.id}/versions")).json()
    newest = max(versions, key=lambda v: v["version"])
    assert newest["version"] == 2 and run["version_id"] == newest["id"] and calls == []
    assert run["status"] == RunStatus.completed and run["reused_from_id"] is not None
    async with pg.session_scope() as session:
        hashes = set(
            await session.scalars(
                sa.select(SyllabusVersion.sha256).where(SyllabusVersion.syllabus_id == syllabus.id)
            )
        )
    assert len(hashes) == 1 and None not in hashes  # learned when the files were read


async def test_the_administrator_can_make_the_ai_check_an_unchanged_file(ref, client_for, monkeypatch):
    syllabus, version = await _uploaded(ref, client_for)
    root = await _root(client_for)
    calls = _count_ai_calls(monkeypatch)

    forced = await root.post(f"/versions/{version['id']}/runs", params={"force": True})

    assert forced.status_code == 201, forced.text
    assert forced.json()["reused_from_id"] is None and forced.json()["force"] is True
    assert len(calls) == 1
    for name in ("shalkar", "provost", "admin.digital"):
        refused = await client_for(ref["users"][name]).post(
            f"/versions/{version['id']}/runs", params={"force": True}
        )
        assert refused.status_code == 403, name


async def test_a_session_takes_unchanged_results_unless_forced(ref, client_for, monkeypatch):
    await _uploaded(ref, client_for)
    root = await _root(client_for)
    body = {"scope_type": "school", "scope_ref": str(ref["schools"]["digital"].id), "semester": "Fall 2026"}

    estimate = (await root.post("/sessions/estimate", json=body)).json()
    forced_estimate = (await root.post("/sessions/estimate", json={**body, "force": True})).json()
    assert estimate["unchanged"] == 1 and estimate["already_read"] == 0
    assert forced_estimate["unchanged"] == 0 and forced_estimate["already_read"] == 1
    provost = client_for(ref["users"]["provost"])
    assert (await provost.post("/sessions/estimate", json={**body, "force": True})).status_code == 403

    calls = _count_ai_calls(monkeypatch)
    plain = await root.post("/sessions", json={**body, "name": "Again"})
    assert plain.status_code == 201, plain.text
    assert calls == []
    forced = await root.post("/sessions", json={**body, "name": "Forced", "force": True})
    assert forced.status_code == 201 and forced.json()["force"] is True
    assert len(calls) == 1
    async with pg.session_scope() as session:
        runs = (await session.scalars(sa.select(AssessmentRun).order_by(AssessmentRun.created_at))).all()
    assert [r.reused_from_id is not None for r in runs] == [False, True, False]
