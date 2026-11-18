"""The AI traces on the Admin screen (/admin/traces, app/services/traces.py)."""

import uuid
from datetime import UTC, datetime, timedelta

from app.documents import LlmTrace
from tests.test_admin import _admin


async def _trace(**fields) -> LlmTrace:
    defaults = {
        "purpose": "criteria",
        "model": "qwen/qwen3",
        "messages": [{"role": "system", "content": "rules"}, {"role": "user", "content": "the syllabus"}],
        "response": '{"ok": true}',
        "prompt_tokens": 1000,
        "completion_tokens": 200,
        "cost_usd": 0.01,
        "duration_ms": 1500,
    }
    return await LlmTrace(**{**defaults, **fields}).insert()


async def test_admin_lists_and_opens_ai_traces(ref, client_for):
    root = await _admin(client_for)
    assert (await client_for(ref["users"]["provost"]).get("/admin/traces")).status_code == 403

    run = uuid.uuid4()
    await _trace(run_id=run, created_at=datetime.now(UTC) - timedelta(days=10))
    await _trace(purpose="translate", model="google/gemma-3", cost_usd=None)
    failed = await _trace(
        purpose="ocr", error="TimeoutError: no answer", response=None, completion_tokens=None
    )

    listed = await root.get("/admin/traces")
    assert listed.status_code == 200, listed.text
    body = listed.json()
    assert body["total"] == 3
    assert [t["purpose"] for t in body["items"]] == ["ocr", "translate", "criteria"]  # newest first
    assert "messages" not in body["items"][0]
    assert body["summary"] == {
        "calls": 3,
        "errors": 1,
        "prompt_tokens": 3000,
        "completion_tokens": 400,
        "cost_usd": 0.02,
        "duration_ms": 4500,
    }
    assert body["purposes"] == ["criteria", "ocr", "translate"]
    assert body["models"] == ["google/gemma-3", "qwen/qwen3"]

    only_errors = (await root.get("/admin/traces", params={"errors": "true"})).json()
    assert [t["id"] for t in only_errors["items"]] == [str(failed.id)]
    assert (await root.get("/admin/traces", params={"days": 7})).json()["total"] == 2
    assert (await root.get("/admin/traces", params={"run_id": str(run)})).json()["total"] == 1
    assert (await root.get("/admin/traces", params={"model": "google/gemma-3"})).json()["total"] == 1
    page = (await root.get("/admin/traces", params={"limit": 1, "offset": 1})).json()
    assert [t["purpose"] for t in page["items"]] == ["translate"] and page["total"] == 3

    detail = await root.get(f"/admin/traces/{failed.id}")
    assert detail.status_code == 200, detail.text
    assert detail.json()["messages"][1] == {"role": "user", "content": "the syllabus"}
    assert detail.json()["error"] == "TimeoutError: no answer"
    assert (await root.get("/admin/traces/not-an-id")).status_code == 404
