"""What a check would read and roughly what it would cost, shown before anyone starts it.

The syllabi come from the same scope and filters as the check itself (sessions.scope_versions). Each file is
one of:
- already read: an earlier check extracted its text (ExtractedDocument), which is reused at no reading cost;
- text: a Word file or a PDF with a text layer;
- scan: a PDF without text, which the OCR model reads page by page first (app.ai.ocr);
- unreadable: a type the checker does not take, an empty file, or one that does not open: no model is called;
- not looked at: not opened within the time the estimate allows (Drive downloads are slow); costed by the
  average of the others, and the range shows what it would be if none or all of them were scans.

A file is opened once to learn which it is (FileProbe in Mongo); versions are immutable, so the answer is kept.

The prices are measured, not guessed: the tokens the last 30 days of checks used per syllabus (LlmTrace), at
OpenRouter's published price of each model, or at what OpenRouter reported charging when the trace has it. With
too little history, the ESTIMATE_* settings stand in. Models on the university's own server cost nothing per use.
"""

import asyncio
import logging
import math
import time
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

from app.ai import structure
from app.ai.extraction import ExtractionError, NoTextError, extract
from app.config import get_settings
from app.documents import ExtractedDocument, FileProbe, LlmTrace
from app.enums import SessionStatus
from app.models import CheckSession, Course, SessionItem, Syllabus, SyllabusVersion
from app.storage import files

log = logging.getLogger(__name__)

SUPPORTED = {".pdf", ".docx"}
# Files opened at once, and how long the estimate waits for them before costing the rest by average.
PROBE_CONCURRENCY = 6
PROBE_BUDGET_S = 20.0
# With no scan opened yet, a scan is assumed to be this long (the Law School's scans averaged about 8 pages).
DEFAULT_SCAN_PAGES = 8
HISTORY_DAYS = 30
MIN_HISTORY_RUNS = 3
LISTED = 50  # file names returned per group


@dataclass
class UnitCosts:
    billed: bool
    check_usd: float  # checking one syllabus against the criteria, once its text is known
    reading_usd: float  # reading a document's structure (outcomes, weeks, assessment) the first time
    ocr_page_usd: float  # one scanned page
    seconds_per_file: float


_units: tuple[float, UnitCosts] | None = None
_prices: tuple[float, dict[str, tuple[float, float]]] | None = None


def _billed() -> bool:
    s = get_settings()
    if s.llm_provider == "fake":
        return False
    return s.llm_provider == "openrouter" or "openrouter.ai" in s.llm_base_url


async def _openrouter_prices() -> dict[str, tuple[float, float]]:
    """{model id: (US dollars per input token, per output token)} from OpenRouter's public model list, kept
    for a day. Empty when it cannot be reached; the ESTIMATE_* settings then stand in."""
    global _prices
    if _prices and time.monotonic() - _prices[0] < 24 * 3600:
        return _prices[1]
    prices: dict[str, tuple[float, float]] = {}
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            response = await client.get("https://openrouter.ai/api/v1/models")
            response.raise_for_status()
            for model in response.json().get("data", []):
                pricing = model.get("pricing") or {}
                try:
                    prices[model["id"]] = (float(pricing["prompt"]), float(pricing["completion"]))
                except (KeyError, TypeError, ValueError):
                    continue
    except (httpx.HTTPError, ValueError) as exc:
        log.warning("OpenRouter prices unavailable: %s", exc)
        return {}
    _prices = (time.monotonic(), prices)
    return prices


def _group_cost(group: dict[str, Any], prices: dict[str, tuple[float, float]]) -> float | None:
    """Total cost of a group of traces: what OpenRouter reported where it did, else tokens at the list price."""
    if group["costed"]:
        return group["cost"] / group["costed"] * group["calls"]
    price = prices.get(group["_id"]["model"])
    if price is None:
        return None
    return group["tokens_in"] * price[0] + group["tokens_out"] * price[1]


async def _traces(match: dict[str, Any]) -> list[dict[str, Any]]:
    collection = LlmTrace.get_pymongo_collection()
    pipeline = [
        {"$match": match},
        {
            "$group": {
                "_id": {"purpose": "$purpose", "model": "$model"},
                "calls": {"$sum": 1},
                "tokens_in": {"$sum": {"$ifNull": ["$prompt_tokens", 0]}},
                "tokens_out": {"$sum": {"$ifNull": ["$completion_tokens", 0]}},
                "cost": {"$sum": {"$ifNull": ["$cost_usd", 0]}},
                "costed": {"$sum": {"$cond": [{"$gt": ["$cost_usd", None]}, 1, 0]}},
            }
        },
    ]
    return [g async for g in await collection.aggregate(pipeline)]


async def _seconds_per_file(session: AsyncSession) -> float:
    """Median time per syllabus of the checks finished lately (start to finish over its files)."""
    since = datetime.now(UTC) - timedelta(days=60)
    rows = (
        await session.execute(
            sa.select(CheckSession.started_at, CheckSession.completed_at, sa.func.count(SessionItem.id))
            .join(SessionItem, SessionItem.session_id == CheckSession.id)
            .where(
                CheckSession.status == SessionStatus.completed,
                CheckSession.started_at >= since,
                CheckSession.completed_at.is_not(None),
            )
            .group_by(CheckSession.id)
            .having(sa.func.count(SessionItem.id) >= 5)
        )
    ).all()
    rates = sorted((done - started).total_seconds() / n for started, done, n in rows if done and started)
    return rates[len(rates) // 2] if rates else get_settings().estimate_seconds_per_file


async def unit_costs(session: AsyncSession) -> UnitCosts:
    """What one syllabus, one first reading and one scanned page cost lately; kept for an hour."""
    global _units
    if not _billed():
        return UnitCosts(False, 0.0, 0.0, 0.0, await _seconds_per_file(session))
    if _units and time.monotonic() - _units[0] < 3600:
        return _units[1]
    s = get_settings()
    seconds = await _seconds_per_file(session)
    prices = await _openrouter_prices()
    since = datetime.now(UTC) - timedelta(days=HISTORY_DAYS)
    check_usd, reading_usd, page_usd = s.estimate_check_usd, s.estimate_reading_usd, s.estimate_ocr_page_usd
    try:
        per_run = {"created_at": {"$gte": since}, "error": None, "run_id": {"$ne": None}}
        runs = len(await LlmTrace.get_pymongo_collection().distinct("run_id", per_run))
        if runs >= MIN_HISTORY_RUNS:
            total = 0.0
            for group in await _traces({**per_run, "purpose": {"$nin": ["ocr", "structure"]}}):
                cost = _group_cost(group, prices)
                if cost is None and group["_id"]["model"] == s.decision_model:
                    cost = group["calls"] * s.estimate_decision_usd
                if cost is None:  # a model with no list price: the chat model's
                    price = prices.get(s.llm_model, (0.2e-6, 0.4e-6))
                    cost = group["tokens_in"] * price[0] + group["tokens_out"] * price[1]
                total += cost
            check_usd = total / runs
        others = {"created_at": {"$gte": since}, "error": None, "purpose": {"$in": ["ocr", "structure"]}}
        for group in await _traces(others):
            cost = _group_cost(group, prices)
            if cost is None or not group["calls"]:
                continue
            if group["_id"]["purpose"] == "ocr":
                page_usd = cost / group["calls"]
            else:
                reading_usd = cost / group["calls"]
    except Exception:  # the estimate must still answer without history
        log.exception("could not measure check costs from the traces; using the ESTIMATE_* settings")
    units = UnitCosts(True, check_usd, reading_usd, page_usd, seconds)
    _units = (time.monotonic(), units)
    return units


def _inspect(path: Path) -> tuple[str, int | None, str | None]:
    """(kind, pages, reason) of a file on disk, the way the check itself will find it."""
    try:
        extract(path)
        return "text", None, None
    except NoTextError:
        if path.suffix.lower() != ".pdf":
            return "unreadable", None, "The file is empty"
        import pypdfium2 as pdfium

        try:
            document = pdfium.PdfDocument(str(path))
        except pdfium.PdfiumError:
            return "unreadable", None, "The file is damaged and does not open"
        try:
            return "scan", len(document), None
        finally:
            document.close()
    except ExtractionError:
        return "unreadable", None, "The file is damaged and does not open"


async def _probe(version: SyllabusVersion) -> FileProbe | None:
    """Open the version's file once and remember what it is. None when it could not be fetched now (a Drive
    download failing is not the file's fault; it is tried again next time)."""
    suffix = Path(version.filename).suffix.lower()
    reason: str | None
    if suffix not in SUPPORTED:
        kind, pages, reason = "unreadable", None, "Only Word (.docx) and PDF files are checked"
    else:
        try:
            async with files.local_copy(version) as path:
                kind, pages, reason = await asyncio.to_thread(_inspect, path)
        except Exception as exc:
            log.info("could not open %s for the estimate: %s", version.filename, exc)
            return None
    probe = FileProbe(version_id=version.id, kind=kind, pages=pages, reason=reason)  # type: ignore[arg-type]
    try:
        await probe.insert()
    except Exception:  # another estimate stored it first
        log.debug("probe for %s already stored", version.id, exc_info=True)
    return probe


async def _probe_all(versions: list[SyllabusVersion], budget_s: float) -> dict[uuid.UUID, FileProbe]:
    gate = asyncio.Semaphore(PROBE_CONCURRENCY)

    async def one(version: SyllabusVersion) -> FileProbe | None:
        async with gate:
            return await _probe(version)

    tasks = [asyncio.create_task(one(v)) for v in versions]
    if not tasks:
        return {}
    done, pending = await asyncio.wait(tasks, timeout=budget_s)
    for task in pending:
        task.cancel()
    found = {}
    for task in done:
        if not task.cancelled() and task.exception() is None and (probe := task.result()):
            found[probe.version_id] = probe
    return found


def _money(value: float) -> float:
    return round(value, 4)


async def estimate(
    session: AsyncSession, pairs: list[tuple[uuid.UUID, uuid.UUID]], budget_s: float = PROBE_BUDGET_S
) -> dict[str, Any]:
    s = get_settings()
    version_ids = [version_id for _, version_id in pairs]
    rows = (
        await session.execute(
            sa.select(SyllabusVersion, sa.func.coalesce(Course.discipline, Course.name))
            .join(Syllabus, Syllabus.id == SyllabusVersion.syllabus_id)
            .join(Course, Course.id == Syllabus.course_id)
            .where(SyllabusVersion.id.in_(version_ids))
        )
    ).all()
    versions = {v.id: (v, course) for v, course in rows}
    wanted = structure.wanted_extractor()
    read_before = {
        d.version_id
        async for d in ExtractedDocument.find({"version_id": {"$in": version_ids}, "extractor": wanted})
    }
    rest = [v for vid, (v, _) in versions.items() if vid not in read_before]
    probes = {p.version_id: p async for p in FileProbe.find({"version_id": {"$in": [v.id for v in rest]}})}
    probes |= await _probe_all([v for v in rest if v.id not in probes], budget_s)

    ocr_on = bool(s.ocr_model) and s.llm_provider != "fake"
    text = scans = pages = unreadable = unknown = 0
    scan_files: list[dict[str, Any]] = []
    unreadable_files: list[dict[str, Any]] = []
    for version in rest:
        course = versions[version.id][1]
        probe = probes.get(version.id)
        if probe is None:
            unknown += 1
        elif probe.kind == "text":
            text += 1
        elif probe.kind == "scan" and ocr_on:
            scans += 1
            read = min(probe.pages or DEFAULT_SCAN_PAGES, s.ocr_max_pages)
            pages += read
            scan_files.append({"filename": version.filename, "course": course, "pages": read})
        else:
            unreadable += 1
            reason = (
                probe.reason if probe.kind == "unreadable" else "A scan, and reading scans is switched off"
            )
            unreadable_files.append({"filename": version.filename, "course": course, "reason": reason})

    units = await unit_costs(session)
    already = len(read_before)
    check_usd = (already + text + scans) * units.check_usd
    reading_usd = (text + scans) * units.reading_usd
    ocr_usd = pages * units.ocr_page_usd
    # The files not looked at: like the ones that were, at the low end none a scan, at the high end all.
    opened = text + scans + unreadable
    scan_share = scans / opened if opened else 0.1
    readable_share = (text + scans) / opened if opened else 1.0
    scan_pages = pages / scans if scans else DEFAULT_SCAN_PAGES
    plain = units.check_usd + units.reading_usd
    scan_extra = scan_pages * units.ocr_page_usd if ocr_on else 0.0
    known = check_usd + reading_usd + ocr_usd
    expected_unknown = unknown * (readable_share * plain + scan_share * scan_extra)
    checked = already + text + scans + unknown
    seconds = checked * units.seconds_per_file
    scan_files.sort(key=lambda f: -(f["pages"] or 0))
    return {
        "files": len(versions),
        "text_files": text,
        "scans": scans,
        "scan_pages": pages,
        "already_read": already,
        "unreadable": unreadable,
        "not_looked_at": unknown,
        "ocr_enabled": ocr_on,
        "billed": units.billed,
        "cost_usd": _money(known + expected_unknown),
        "cost_min_usd": _money(known),
        "cost_max_usd": _money(known + unknown * (plain + scan_extra)),
        "check_usd": _money(check_usd + unknown * readable_share * units.check_usd),
        "reading_usd": _money(reading_usd + unknown * readable_share * units.reading_usd),
        "ocr_usd": _money(ocr_usd + unknown * scan_share * scan_extra),
        "minutes": max(1, math.ceil(seconds / 60)) if checked else 0,
        "scan_files": scan_files[:LISTED],
        "unreadable_files": unreadable_files[:LISTED],
    }
