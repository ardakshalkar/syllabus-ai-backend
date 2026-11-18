"""The AI traces on the Admin screen: every request sent to an AI model, as stored in llm_traces
(app.ai.llm._save_trace), with the course of its run looked up in Postgres."""

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import sqlalchemy as sa
from bson import ObjectId
from bson.errors import InvalidId
from fastapi import HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.documents import LlmTrace
from app.models import AssessmentRun, Course, Syllabus, SyllabusVersion
from app.schemas.admin import TraceDetailOut, TraceRowOut, TracesOut, TraceSummaryOut

# Everything but the prompt and the answer, which can hold the whole syllabus.
_ROW_FIELDS = {"messages": 0, "response": 0}


def _match(
    *, purpose: str | None, model: str | None, run_id: uuid.UUID | None, errors: bool | None, days: int | None
) -> dict[str, Any]:
    match: dict[str, Any] = {}
    if purpose:
        match["purpose"] = purpose
    if model:
        match["model"] = model
    if run_id:
        match["run_id"] = run_id
    if errors is True:
        match["error"] = {"$ne": None}
    elif errors is False:
        match["error"] = None
    if days:
        match["created_at"] = {"$gte": datetime.now(UTC) - timedelta(days=days)}
    return match


async def _courses(session: AsyncSession, run_ids: set[uuid.UUID]) -> dict[uuid.UUID, tuple[uuid.UUID, str]]:
    if not run_ids:
        return {}
    rows = await session.execute(
        sa.select(AssessmentRun.id, Syllabus.id, Course.name)
        .join(SyllabusVersion, SyllabusVersion.id == AssessmentRun.version_id)
        .join(Syllabus, Syllabus.id == SyllabusVersion.syllabus_id)
        .join(Course, Course.id == Syllabus.course_id)
        .where(AssessmentRun.id.in_(run_ids))
    )
    return {run: (syllabus, course) for run, syllabus, course in rows.all()}


def _row(doc: dict[str, Any], courses: dict[uuid.UUID, tuple[uuid.UUID, str]]) -> dict[str, Any]:
    syllabus_id, course = courses.get(doc.get("run_id"), (None, None))
    created = doc["created_at"]
    return {
        "id": str(doc["_id"]),
        "created_at": created if created.tzinfo else created.replace(tzinfo=UTC),
        "purpose": doc["purpose"],
        "model": doc["model"],
        "provider": doc.get("provider"),
        "run_id": doc.get("run_id"),
        "syllabus_id": syllabus_id,
        "course": course,
        "prompt_tokens": doc.get("prompt_tokens"),
        "completion_tokens": doc.get("completion_tokens"),
        "cached_tokens": doc.get("cached_tokens"),
        "cost_usd": doc.get("cost_usd"),
        "duration_ms": doc.get("duration_ms") or 0,
        "queued_ms": doc.get("queued_ms"),
        "error": doc.get("error"),
    }


async def list_traces(
    session: AsyncSession,
    *,
    purpose: str | None = None,
    model: str | None = None,
    run_id: uuid.UUID | None = None,
    errors: bool | None = None,
    days: int | None = None,
    limit: int = 50,
    offset: int = 0,
) -> TracesOut:
    collection = LlmTrace.get_pymongo_collection()
    match = _match(purpose=purpose, model=model, run_id=run_id, errors=errors, days=days)
    # Newest first by _id: an ObjectId starts with its creation time and is always indexed.
    docs = [d async for d in collection.find(match, _ROW_FIELDS).sort("_id", -1).skip(offset).limit(limit)]
    pipeline = [
        {"$match": match},
        {
            "$group": {
                "_id": None,
                "calls": {"$sum": 1},
                "errors": {"$sum": {"$cond": [{"$gt": ["$error", None]}, 1, 0]}},
                "prompt_tokens": {"$sum": {"$ifNull": ["$prompt_tokens", 0]}},
                "completion_tokens": {"$sum": {"$ifNull": ["$completion_tokens", 0]}},
                "cost_usd": {"$sum": {"$ifNull": ["$cost_usd", 0]}},
                "duration_ms": {"$sum": {"$ifNull": ["$duration_ms", 0]}},
            }
        },
    ]
    groups = [g async for g in await collection.aggregate(pipeline)]
    total = groups[0] if groups else {}
    courses = await _courses(session, {d["run_id"] for d in docs if d.get("run_id")})
    return TracesOut(
        items=[TraceRowOut(**_row(d, courses)) for d in docs],
        total=total.get("calls", 0),
        limit=limit,
        offset=offset,
        summary=TraceSummaryOut(
            calls=total.get("calls", 0),
            errors=total.get("errors", 0),
            prompt_tokens=total.get("prompt_tokens", 0),
            completion_tokens=total.get("completion_tokens", 0),
            cost_usd=total.get("cost_usd", 0.0),
            duration_ms=total.get("duration_ms", 0),
        ),
        purposes=sorted(p for p in await collection.distinct("purpose") if p),
        models=sorted(m for m in await collection.distinct("model") if m),
    )


async def get_trace(session: AsyncSession, trace_id: str) -> TraceDetailOut:
    try:
        oid = ObjectId(trace_id)
    except (InvalidId, TypeError):
        oid = None
    doc = await LlmTrace.get_pymongo_collection().find_one({"_id": oid}) if oid else None
    if doc is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No such AI trace")
    courses = await _courses(session, {doc["run_id"]} if doc.get("run_id") else set())
    messages = doc.get("messages") or []
    return TraceDetailOut(
        **_row(doc, courses),
        messages=[m if isinstance(m, dict) else {"role": "?", "content": str(m)} for m in messages],
        response=doc.get("response"),
    )
