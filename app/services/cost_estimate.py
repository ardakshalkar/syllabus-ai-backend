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
too little history, the ESTIMATE_* settings stand in. Models on the university's own server cost nothing per use;
which server is which is worked out in services.ai_servers.

Checking is priced for the check mode the administrator chose (services.check_mode), from the runs of check
sessions that ran in that mode; a mode with too little history is priced from its model's list price
(ESTIMATE_SINGLE_TOKENS_*) or, for "standard", from ESTIMATE_CHECK_USD.
"""

import asyncio
import logging
import math
import time
import uuid
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

from app.ai import ocr, structure
from app.ai.extraction import ExtractionError, NoTextError, extract
from app.config import get_settings
from app.documents import ExtractedDocument, FileProbe, LlmTrace
from app.enums import SessionStatus
from app.models import AssessmentRun, CheckSession, Course, SessionItem, Syllabus, SyllabusVersion
from app.services import ai_servers, check_mode
from app.services.check_mode import CheckMode
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
# Tokens (in, out) per syllabus where no history says otherwise, measured on the Law School's 22 syllabi (Nov
# 2026): reading its structure, sorting its lines (only with BLOCKS_SORT_BY_MODEL), translating its findings.
READ_TOKENS = (8_000, 1_300)
SORT_TOKENS = (9_000, 2_300)
TRANSLATE_TOKENS = (2_600, 5_100)
# The traces that read a document once per version, priced as reading rather than as checking: they carry no
# run_id ("blocks" once cost as much as the check itself and was counted nowhere).
READING = ("structure", "blocks")


@dataclass
class UnitCosts:
    billed: bool
    check_usd: float  # checking one syllabus against the criteria, once its text is known
    reading_usd: float  # reading a document's structure (outcomes, weeks, assessment) the first time
    ocr_page_usd: float  # one scanned page
    seconds_per_file: float
    check_measured: bool = False  # check_usd comes from recent checks in this mode, not from list prices


# Per check mode (services.check_mode), each kept for an hour.
_units: dict[CheckMode, tuple[float, UnitCosts]] = {}
_prices: tuple[float, dict[str, tuple[float, float]]] | None = None


async def servers(mode: CheckMode) -> list[ai_servers.AiServer]:
    """The servers a check in `mode` talks to: the chat server, and the decision service in the standard mode."""
    chat = await ai_servers.chat_server()
    if mode.mode == check_mode.SINGLE and mode.model and chat.kind != "test":
        chat = replace(chat, model=mode.model)
    found = [chat]
    if mode.mode != check_mode.SINGLE and (decisions := await ai_servers.decision_server()):
        found.append(decisions)
    return found


async def billed(mode: CheckMode | None = None) -> bool:
    """Whether a check in `mode` (by default the standard one) costs money per use on any of its servers."""
    return any(server.billed for server in await servers(mode or CheckMode()))


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
    price = ai_servers.price_of(group["_id"]["model"], prices)
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


async def _runs_in_mode(session: AsyncSession, mode: CheckMode, since: datetime) -> list[uuid.UUID]:
    """The check runs of the last HISTORY_DAYS that ran in `mode` (their session's mode; a session from
    before the setting existed ran in "standard")."""
    query = (
        sa.select(AssessmentRun.id)
        .join(CheckSession, CheckSession.id == AssessmentRun.session_id)
        .where(AssessmentRun.created_at >= since)
    )
    if mode.mode == check_mode.SINGLE:
        query = query.where(CheckSession.check_mode == mode.mode, CheckSession.check_model == mode.model)
    else:
        query = query.where(
            sa.or_(CheckSession.check_mode.is_(None), CheckSession.check_mode == check_mode.STANDARD)
        )
    return list((await session.scalars(query)).all())


def _at(tokens: tuple[float, float], model: str, prices: dict[str, tuple[float, float]]) -> float | None:
    price = ai_servers.price_of(model, prices)
    return None if price is None else tokens[0] * price[0] + tokens[1] * price[1]


def _listed_check_usd(mode: CheckMode, prices: dict[str, tuple[float, float]]) -> float | None:
    """A one-request check at its model's list price, before there is history for it, with the translation of its
    findings (TRANSLATION_MODEL, or the check's model) that follows every check."""
    if mode.mode != check_mode.SINGLE or not mode.model:
        return None
    s = get_settings()
    check = _at((s.estimate_single_tokens_in, s.estimate_single_tokens_out), mode.model, prices)
    if check is None:
        return None
    if s.translate_results:
        check += _at(TRANSLATE_TOKENS, s.translation_model or mode.model, prices) or 0.0
    return check


def _reading_usd(
    tokens: dict[str, tuple[float, float]], prices: dict[str, tuple[float, float]]
) -> float | None:
    """Reading one new document at the prices of the models that will read it: its structure (STRUCTURE_MODEL or
    the chat model) and, with BLOCKS_SORT_BY_MODEL, the sorting of its lines (the chat model). `tokens` holds
    the tokens one call took lately by purpose; READ_TOKENS and SORT_TOKENS stand in for a purpose with none."""
    s = get_settings()
    total = _at(tokens.get("structure", READ_TOKENS), s.structure_model or s.llm_model, prices)
    if total is not None and s.blocks_sort_by_model and s.explain_blocks:
        total += _at(tokens.get("blocks", SORT_TOKENS), s.llm_model, prices) or 0.0
    return total


async def unit_costs(session: AsyncSession, mode: CheckMode | None = None) -> UnitCosts:
    """What one syllabus (checked in `mode`, by default the administrator's current one), one first reading
    and one scanned page cost lately; kept for an hour."""
    if mode is None:
        mode = await check_mode.current(session)
    found = await servers(mode)
    if not any(server.billed for server in found):
        return UnitCosts(False, 0.0, 0.0, 0.0, await _seconds_per_file(session))
    # The chat server may be the university's own while the decision service is paid: then only decisions cost.
    chat_paid = found[0].billed
    cached = _units.get(mode)
    if cached and time.monotonic() - cached[0] < 3600:
        return cached[1]
    s = get_settings()
    seconds = await _seconds_per_file(session)
    prices = await _openrouter_prices()
    since = datetime.now(UTC) - timedelta(days=HISTORY_DAYS)
    check_usd, reading_usd, page_usd = s.estimate_check_usd, s.estimate_reading_usd, s.estimate_ocr_page_usd
    if not chat_paid:
        check_usd = s.estimate_decision_usd if len(found) > 1 else 0.0
        reading_usd = page_usd = 0.0
    listed = _listed_check_usd(mode, prices)
    if listed is not None:
        check_usd = listed
    measured = False
    try:
        run_ids = await _runs_in_mode(session, mode, since)
        per_run = {"created_at": {"$gte": since}, "error": None, "run_id": {"$in": run_ids}}
        runs = len(await LlmTrace.get_pymongo_collection().distinct("run_id", per_run)) if run_ids else 0
        if runs >= MIN_HISTORY_RUNS:
            measured = True
            total = 0.0
            for group in await _traces({**per_run, "purpose": {"$nin": ["ocr", *READING]}}):
                decision = group["_id"]["model"] == s.decision_model
                if not chat_paid and not decision:
                    continue
                cost = _group_cost(group, prices)
                if cost is None and decision:
                    cost = group["calls"] * s.estimate_decision_usd
                if cost is None:  # a model with no list price: the chat model's
                    price = ai_servers.price_of(s.llm_model, prices) or (0.2e-6, 0.4e-6)
                    cost = group["tokens_in"] * price[0] + group["tokens_out"] * price[1]
                total += cost
            check_usd = total / runs
        others = {"created_at": {"$gte": since}, "error": None, "purpose": {"$in": ["ocr", *READING]}}
        reads: dict[str, list[float]] = {}  # purpose -> [calls, tokens in, tokens out], over every model
        for group in await _traces(others) if chat_paid else []:
            purpose = group["_id"]["purpose"]
            if purpose != "ocr":
                totals = reads.setdefault(purpose, [0, 0, 0])
                for i, key in enumerate(("calls", "tokens_in", "tokens_out")):
                    totals[i] += group[key]
                continue
            cost = _group_cost(group, prices)
            if cost is not None and group["calls"]:
                page_usd = cost / group["calls"]
        if chat_paid:
            # Priced by tokens at today's reading models, not by what earlier reads cost: those may have run on
            # another model (a free one, or the chat model before STRUCTURE_MODEL was set).
            tokens = {p: (t[1] / t[0], t[2] / t[0]) for p, t in reads.items() if t[0] and t[1]}
            reading = _reading_usd(tokens, prices)
            if reading is not None:
                reading_usd = reading
    except Exception:  # the estimate must still answer without history
        log.exception("could not measure check costs from the traces; using the ESTIMATE_* settings")
    units = UnitCosts(True, check_usd, reading_usd, page_usd, seconds, measured)
    _units[mode] = (time.monotonic(), units)
    return units


def _broken_pdf(path: Path) -> bool:
    """A PDF whose text layer is broken, which the check sends to OCR like a scan (app.ai.structure)."""
    if path.suffix.lower() != ".pdf" or not ocr.enabled():
        return False
    from app.ai.markdown import looks_broken, to_markdown

    try:
        return looks_broken(to_markdown(path))
    except ExtractionError:
        return False


def _inspect(path: Path) -> tuple[str, int | None, str | None]:
    """(kind, pages, reason) of a file on disk, the way the check itself will find it."""
    try:
        extract(path)
        if not _broken_pdf(path):
            return "text", None, None
    except NoTextError:
        if path.suffix.lower() != ".pdf":
            return "unreadable", None, "The file is empty"
    except ExtractionError:
        return "unreadable", None, "The file is damaged and does not open"
    # A scan, or a PDF with a broken text layer: the OCR model reads both page by page.
    import pypdfium2 as pdfium

    try:
        document = pdfium.PdfDocument(str(path))
    except pdfium.PdfiumError:
        return "unreadable", None, "The file is damaged and does not open"
    try:
        return "scan", len(document), None
    finally:
        document.close()


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

    mode = await check_mode.current(session)
    units = await unit_costs(session, mode)
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
        "servers": [server.out() for server in await servers(mode)],
        "cost_usd": _money(known + expected_unknown),
        "cost_min_usd": _money(known),
        "cost_max_usd": _money(known + unknown * (plain + scan_extra)),
        "check_usd": _money(check_usd + unknown * readable_share * units.check_usd),
        "reading_usd": _money(reading_usd + unknown * readable_share * units.reading_usd),
        "ocr_usd": _money(ocr_usd + unknown * scan_share * scan_extra),
        "minutes": max(1, math.ceil(seconds / 60)) if checked else 0,
        "check_mode": mode.mode,
        "check_model": mode.model,
        "check_mode_label": mode.label,
        "check_per_syllabus_usd": _money(units.check_usd),
        "check_price_measured": units.check_measured,
        "scan_files": scan_files[:LISTED],
        "unreadable_files": unreadable_files[:LISTED],
    }


async def mode_options(session: AsyncSession) -> list[dict[str, Any]]:
    """Every check mode the administrator can choose, with what checking one syllabus costs in it."""
    modes = [CheckMode()] + [CheckMode(check_mode.SINGLE, m) for m in check_mode.allowed_models()]
    options = []
    for mode in modes:
        units = await unit_costs(session, mode)
        options.append(
            {
                "mode": mode.mode,
                "model": mode.model,
                "label": mode.label,
                "per_syllabus_usd": _money(units.check_usd),
                "measured": units.check_measured,
            }
        )
    return options
