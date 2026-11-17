"""OCR cost controls (output cap, a refused key, the page cache) and the cost estimate before a check."""

import asyncio
import time

import pytest

from app.config import get_settings
from seed.samples import sample_syllabus
from tests.conftest import create_syllabus, upload
from tests.test_pipeline import _scanned_pdf

PDF = "application/pdf"


def test_ocr_loop_on_empty_cells_is_trimmed():
    from app.ai import ocr

    looped = "| Апта | Тақырып |\n|---|---|\n| 1 | Кіріспе |\n" + "| | |\n" * 40 + "Соңы " + "| " * 2000
    assert ocr.clean(looped) == "| Апта | Тақырып |\n|---|---|\n| 1 | Кіріспе |\nСоңы |"


async def test_refused_key_stops_the_other_pages(tmp_path):
    from app.ai import ocr
    from app.ai.llm import KeyRefusedError

    finished: list[int] = []

    async def transcribe(image: bytes, number: int) -> str:
        if number == 1:
            raise KeyRefusedError("the LLM server refused the API key (403): Key limit exceeded")
        await asyncio.sleep(5)
        finished.append(number)
        return "text"

    started = time.monotonic()
    with pytest.raises(KeyRefusedError):
        await ocr.read_pdf(_scanned_pdf(tmp_path, pages=4), transcribe)
    assert time.monotonic() - started < 3
    assert finished == []  # the other pages were cancelled, not left running


def test_refused_key_is_not_retried():
    import httpx
    import openai

    from app.ai.llm import key_refused

    def error(code: int) -> openai.APIStatusError:
        response = httpx.Response(code, request=httpx.Request("POST", "https://openrouter.ai/api/v1/chat"))
        return openai.APIStatusError("refused", response=response, body=None)

    assert key_refused(error(403)) is not None
    assert key_refused(error(402)) is not None
    assert key_refused(error(429)) is None  # rate limited: worth another try
    assert key_refused(error(500)) is None


async def test_scanned_pages_are_paid_for_once(ref, tmp_path, monkeypatch):
    from app.ai import ocr

    monkeypatch.setenv("OCR_MODEL", "test/ocr-cache")
    get_settings.cache_clear()

    calls: list[int] = []

    async def transcribe(image: bytes, number: int) -> str:
        calls.append(number)
        return f"Бет {number}: " + "мәтін " * 30

    path = _scanned_pdf(tmp_path, pages=3)
    first = await ocr.read_pdf(path, transcribe)
    second = await ocr.read_pdf(path, transcribe)
    assert first == second
    assert sorted(calls) == [1, 2, 3]


async def test_estimate_lists_what_a_check_would_read(ref, client_for, tmp_path):
    lead = client_for(ref["users"]["shalkar"])
    checked = await create_syllabus(lead, ref["courses"]["ai"].id)
    await upload(lead, checked["id"], sample_syllabus("ai", with_issues=False))
    scanned = await create_syllabus(client_for(ref["users"]["lee"]), ref["courses"]["ml"].id)
    response = await client_for(ref["users"]["lee"]).post(
        f"/syllabi/{scanned['id']}/versions",
        files={"file": ("scan.pdf", _scanned_pdf(tmp_path, pages=3).read_bytes(), PDF)},
    )
    assert response.status_code == 201, response.text

    admin = client_for(ref["users"]["admin.digital"])
    body = {"scope_type": "school", "scope_ref": str(ref["schools"]["digital"].id), "semester": "Fall 2026"}
    estimate = (await admin.post("/sessions/estimate", json=body)).json()
    assert estimate["files"] == 2
    assert estimate["already_read"] == 1  # read by the check after its upload: reused for free
    # OCR is off in the tests, so the scan is one the check cannot read.
    assert estimate["unreadable"] == 1
    assert estimate["unreadable_files"][0]["filename"] == "scan.pdf"
    assert estimate["billed"] is False and estimate["cost_usd"] == 0

    economics = {**body, "scope_ref": str(ref["schools"]["economics"].id)}
    assert (await admin.post("/sessions/estimate", json=economics)).status_code == 403

    created = await admin.post(
        "/sessions", json={**body, "name": "Digital review", "estimated_cost_usd": 1.25}
    )
    assert created.status_code == 201, created.text
    assert created.json()["estimated_cost_usd"] == 1.25


async def test_estimate_prices_scans_by_page(ref, client_for, tmp_path, monkeypatch):
    from app.documents import FileProbe
    from app.services import cost_estimate

    lee = client_for(ref["users"]["lee"])
    scanned = await create_syllabus(lee, ref["courses"]["ml"].id)
    await lee.post(
        f"/syllabi/{scanned['id']}/versions",
        files={"file": ("scan.pdf", _scanned_pdf(tmp_path, pages=5).read_bytes(), PDF)},
    )
    monkeypatch.setenv("OCR_MODEL", "google/gemini-3.1-flash-lite")
    monkeypatch.setenv("LLM_PROVIDER", "openrouter")
    get_settings.cache_clear()

    async def units(session):
        return cost_estimate.UnitCosts(True, 0.02, 0.002, 0.001, 30)

    monkeypatch.setattr(cost_estimate, "unit_costs", units)
    admin = client_for(ref["users"]["admin.digital"])
    body = {"scope_type": "school", "scope_ref": str(ref["schools"]["digital"].id), "semester": "Fall 2026"}
    estimate = (await admin.post("/sessions/estimate", json=body)).json()
    assert (estimate["scans"], estimate["scan_pages"]) == (1, 5)
    assert estimate["scan_files"] == [
        {"filename": "scan.pdf", "course": "Machine Learning", "pages": 5, "reason": None}
    ]
    assert estimate["ocr_usd"] == pytest.approx(0.01, abs=0.005)
    assert estimate["cost_usd"] == pytest.approx(0.02 + 0.002 + 0.005, abs=0.01)
    assert await FileProbe.find_all().count() == 1  # opened once, remembered
