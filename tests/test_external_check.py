"""A check made by a person's own AI (through the MCP server) is stored as a run the analysis counts."""

import io
import uuid

import pytest

from app.documents import ExtractedDocument
from seed.samples import sample_syllabus
from tests.conftest import create_syllabus, upload


async def _setup(ref, client_for):
    lead = client_for(ref["users"]["shalkar"])
    syllabus = await create_syllabus(lead, ref["courses"]["ai"].id)
    uploaded = await upload(
        lead, syllabus["id"], sample_syllabus("Artificial Intelligence", with_issues=False)
    )
    return syllabus["id"], uploaded["version"]["id"]


async def _codes(client) -> list[str]:
    rubric = (await client.get("/rubric")).json()
    return [c["code"] for p in rubric["principles"] for c in p["criteria"]]


async def _quote(client, version_id) -> str:
    text = (await client.get(f"/versions/{version_id}/text")).json()["text"]
    return next(line.strip(" #*|") for line in text.splitlines() if len(line.strip(" #*|")) > 25)


def _answer(codes, quote, **special):
    results = [
        {
            "criterion": c,
            "status": "Compliant",
            "evidence": quote,
            "explanation": "It is stated.",
            "recommendation": "",
        }
        for c in codes
    ]
    for code, change in special.items():
        index = codes.index(code.replace("_", "."))
        results[index] = {**results[index], **change}
    return {"model": "Claude Opus", "results": results}


async def test_a_full_external_check_becomes_a_run_the_analysis_sees(ref, client_for):
    syllabus_id, version_id = await _setup(ref, client_for)
    provost = client_for(ref["users"]["provost"])
    codes = await _codes(provost)
    quote = await _quote(provost, version_id)
    body = _answer(
        codes,
        quote,
        **{
            codes[0].replace(".", "_"): {
                "status": "Not compliant",
                "explanation": "Nothing about it.",
                "recommendation": "Add it.",
            },
            codes[1].replace(".", "_"): {
                "status": "Compliant",
                "evidence": "a sentence the syllabus never contains",
            },
        },
    )
    response = await provost.post(f"/versions/{version_id}/external-check", json=body)
    assert response.status_code == 201, response.text
    out = response.json()
    assert out["issues"] == 1 and out["unverified"] == [codes[1]]
    assert out["model_version"] == "external: Claude Opus"

    latest = (await provost.get(f"/versions/{version_id}/runs/latest")).json()
    assert (
        latest["id"] == out["run_id"]
        and latest["trigger"] == "External AI"
        and latest["status"] == "Completed"
    )
    results = {r["criterion_id"]: r for r in (await provost.get(f"/runs/{out['run_id']}")).json()["results"]}
    assert results[codes[0]]["status"] == "Not compliant"
    assert results[codes[1]]["status"] == "Cannot verify"  # the invented quote was not accepted
    assert results[codes[2]]["status"] == "Compliant" and results[codes[2]]["evidence_verified"] is True

    syllabus = (await provost.get(f"/syllabi/{syllabus_id}")).json()
    assert syllabus["issues"] == 1
    activity = (await provost.get(f"/syllabi/{syllabus_id}/activity")).json()
    assert any("External AI check (Claude Opus)" in (a.get("comment") or "") for a in activity)
    triggers = (await provost.get("/analytics/runs-by-trigger", params={"semester": "Fall 2026"})).json()
    assert {"trigger": "External AI", "runs": 1} in triggers
    # lists and the detail say who judged; the analysis counts how many of the checked were judged from outside
    listed = (await provost.get("/syllabi", params={"semester": "Fall 2026"})).json()["items"]
    assert [i["external_model"] for i in listed if i["id"] == syllabus_id] == ["Claude Opus"]
    assert all(i["external_model"] is None for i in listed if i["id"] != syllabus_id)
    assert (await provost.get(f"/syllabi/{syllabus_id}")).json()["external_model"] == "Claude Opus"
    # the analysis and the review report (JSON and PDF) take the stored result like any other
    summary = await provost.get("/analysis", params={"semester": "Fall 2026"})
    assert summary.status_code == 200, summary.text
    assert summary.json()["totals"]["external"] == 1
    assert sum(s["external"] for s in summary.json()["schools"]) == 1
    report = await provost.get("/analysis/report", params={"semester": "Fall 2026"})
    assert report.status_code == 200, report.text
    courses = (
        await provost.get("/analysis/report", params={"semester": "Fall 2026", "include": "all"})
    ).json()["courses"]
    assert [c["external_model"] for c in courses if c["syllabus_id"] == syllabus_id] == ["Claude Opus"]
    pdf = await provost.get(
        "/analysis/report.pdf", params={"semester": "Fall 2026", "include": "all", "lang": "ru"}
    )
    assert pdf.status_code == 200 and pdf.content.startswith(b"%PDF")
    pdfminer = pytest.importorskip("pdfminer.high_level")
    page_text = " ".join(pdfminer.extract_text(io.BytesIO(pdf.content)).split())
    assert "Проверено внешним ИИ: Claude Opus" in page_text  # the course page says who judged it


async def test_every_criterion_must_be_answered_once_and_only_real_ones(ref, client_for):
    _, version_id = await _setup(ref, client_for)
    provost = client_for(ref["users"]["provost"])
    codes = await _codes(provost)
    quote = await _quote(provost, version_id)
    body = _answer(codes, quote)
    short = {**body, "results": body["results"][1:]}
    r = await provost.post(f"/versions/{version_id}/external-check", json=short)
    assert r.status_code == 422 and f"missing: {codes[0]}" in r.json()["detail"]
    twice = {**body, "results": [*body["results"], body["results"][0]]}
    r = await provost.post(f"/versions/{version_id}/external-check", json=twice)
    assert r.status_code == 422 and f"given twice: {codes[0]}" in r.json()["detail"]
    odd = {**body, "results": [*body["results"], {**body["results"][0], "criterion": "9.9"}]}
    r = await provost.post(f"/versions/{version_id}/external-check", json=odd)
    assert r.status_code == 422 and "unknown criteria: 9.9" in r.json()["detail"]
    bad_status = {**body, "results": [{**body["results"][0], "status": "Great"}, *body["results"][1:]]}
    assert (await provost.post(f"/versions/{version_id}/external-check", json=bad_status)).status_code == 422
    # nothing was stored by the refused tries
    assert (await provost.get(f"/versions/{version_id}/runs/latest")).json().get("trigger") != "External AI"


async def test_only_those_who_manage_the_school_may_add_one(ref, client_for):
    _, version_id = await _setup(ref, client_for)
    provost = client_for(ref["users"]["provost"])
    body = _answer(await _codes(provost), await _quote(provost, version_id))
    lead = client_for(ref["users"]["shalkar"])  # the course's own leader
    assert (await lead.post(f"/versions/{version_id}/external-check", json=body)).status_code == 403
    stranger = client_for(ref["users"]["sultanov"])  # teaches another course
    assert (await stranger.post(f"/versions/{version_id}/external-check", json=body)).status_code in (
        403,
        404,
    )
    assert (await provost.post(f"/versions/{version_id}/external-check", json=body)).status_code == 201


async def test_a_version_without_text_cannot_be_checked_from_outside(ref, client_for):
    _, version_id = await _setup(ref, client_for)
    provost = client_for(ref["users"]["provost"])
    body = _answer(await _codes(provost), await _quote(provost, version_id))
    doc = await ExtractedDocument.find_one(ExtractedDocument.version_id == uuid.UUID(version_id))
    doc.content.raw_text = ""
    await doc.save()
    r = await provost.post(f"/versions/{version_id}/external-check", json=body)
    assert r.status_code == 409 and "no text" in r.json()["detail"]
