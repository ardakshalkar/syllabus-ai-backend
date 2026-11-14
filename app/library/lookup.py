"""Look up a syllabus's literature in the library, and turn what was found into the criterion's result."""

import hashlib
import logging
import re
from typing import Literal

from pydantic import BaseModel

from app.ai.types import CriterionResultData, CriterionSpec, ExtractedContent
from app.enums import ResultStatus
from app.library import megapro
from app.library.config import LibraryConfig, get_library_config
from app.library.references import STOPWORDS, Reference, literature_lines, split_references, stems
from app.storage import cache

log = logging.getLogger(__name__)

# The rubric's required_context for the criterion this module answers.
CONTEXT = "Narxoz library catalogue"

Found = Literal["held", "no_copies", "not_found", "error", "not_checked"]


class ReferenceResult(BaseModel):
    text: str
    kind: Literal["core", "supplementary"]
    status: Found
    query: str = ""
    record: str = ""
    record_id: str = ""
    copies: int | None = None
    electronic: bool = False
    database: int | None = None
    error: str = ""


class LibraryReport(BaseModel):
    provider: str
    references: list[ReferenceResult]

    def considered(self) -> list[ReferenceResult]:
        """The core titles, or every title when the syllabus does not mark any as core."""
        core = [r for r in self.references if r.kind == "core"]
        return core or self.references


_TOKEN = re.compile(r"[^\W_]+")


def _tokens(text: str) -> list[str]:
    return [t.lower() for t in _TOKEN.findall(text)]


# The record's title ends where its description goes on: "[Текст]", ": учебник", "/ authors", "= parallel title",
# or an English subtitle ("Applications: The Big Ideas…").
_RECORD_TITLE_END = re.compile(r"\s\[|\s?:\s|\s/\s|\s=\s|\.\s-\s")


def _record_title_words(record: megapro.CatalogueRecord) -> list[str]:
    title = _RECORD_TITLE_END.split(record.description, maxsplit=1)[0]
    return [t for t in _tokens(title) if len(t) >= 3 and t not in STOPWORDS]


def _score(ref: Reference, record: megapro.CatalogueRecord, stem_length: int) -> tuple[float, float]:
    """How much of the reference's title the record has, and how much of the record's title the reference
    has. Both must be high: "Маркетинг менеджмент" is in "Маркетинг от А до Я … каждый менеджер" one way only."""
    wanted = stems(ref.title_words[:4], stem_length)
    if not wanted:
        return 0.0, 0.0
    tokens = _tokens(record.description)
    forward = sum(any(t.startswith(w) for t in tokens) for w in wanted) / len(wanted)
    names = ([ref.surname] if ref.surname else []) + ref.name_words
    ours = stems(ref.title_words + names, stem_length)
    theirs = _record_title_words(record)
    backward = sum(any(t.startswith(w) for w in ours) for t in theirs) / len(theirs) if theirs else 0.0
    return forward, backward


def _match_score(
    ref: Reference, record: megapro.CatalogueRecord, config: LibraryConfig, by_isbn: bool
) -> float:
    """0 when the record is not this reference; otherwise higher is a closer match."""
    if by_isbn:
        held_isbn = ref.isbn is not None and ref.isbn in re.sub(r"[^\dX]", "", record.text.upper())
        return 2.0 if held_isbn else 0.0
    forward, backward = _score(ref, record, config.lookup.stem_length)
    if forward < config.lookup.title_match or backward < config.lookup.record_match:
        return 0.0
    names = [ref.surname] if ref.surname else ref.name_words
    tokens = _tokens(record.text)
    if names and not any(t.startswith(n.lower()[:5]) for n in names for t in tokens):
        return 0.0
    return forward + backward


def _queries(ref: Reference, stem_length: int) -> list[tuple[str, bool]]:
    """Most specific first. (terms, is_isbn)."""
    out: list[tuple[str, bool]] = []
    if ref.isbn:
        out.append((ref.isbn, True))
    title = stems(ref.search_words or ref.title_words[:4], stem_length)
    surname = [ref.surname.lower()[:stem_length]] if ref.surname else []
    # Which of "William Vaughan" is the surname is unknown, so each is tried with the start of the title.
    names = [[n.lower()[:stem_length], *title[:2]] for n in reversed(ref.name_words)]
    # A name narrows the search, so it goes first: the title alone can find dozens and only one page is read.
    for terms in (surname + title, *names, title, surname + title[:2]):
        q = " ".join(terms)
        if len(terms) >= 2 and q not in [x for x, _ in out]:
            out.append((q, False))
    return out


def _is_held(record: megapro.CatalogueRecord, config: LibraryConfig) -> bool:
    if record.copies:
        return True
    return config.status.count_electronic and (record.electronic or record.database != 0)


async def _find(
    ref: Reference, opac: megapro.OpacClient | megapro.ApiClient, config: LibraryConfig
) -> ReferenceResult:
    result = ReferenceResult(text=ref.text, kind=ref.kind, status="not_found")
    best: megapro.CatalogueRecord | None = None
    for terms, by_isbn in _queries(ref, config.lookup.stem_length):
        for database in config.megapro_opac.databases:
            _, records = await opac.search(terms, database)
            scored = sorted(
                ((s, r) for r in records if (s := _match_score(ref, r, config, by_isbn)) > 0),
                key=lambda sr: -sr[0],
            )
            matching = [r for _, r in scored]
            held = [r for r in matching if _is_held(r, config)]
            if held or matching:
                best = (held or matching)[0]
                result.query = terms
                if held:
                    break
        if best is not None and _is_held(best, config):
            break
    if best is not None:
        result.status = "held" if _is_held(best, config) else "no_copies"
        result.record, result.record_id = best.description, best.id
        result.copies, result.electronic, result.database = best.copies, best.electronic, best.database
    return result


def _cache_key(ref: Reference, config: LibraryConfig) -> str:
    raw = "|".join([config.provider, ",".join(map(str, config.megapro_opac.databases)), ref.text.lower()])
    return "library:v1:" + hashlib.sha1(raw.encode()).hexdigest()


async def _cached(key: str) -> ReferenceResult | None:
    try:
        data = await cache.get_json(key)
    except Exception:  # the cache only saves requests; without Redis every title is simply looked up
        return None
    return ReferenceResult.model_validate(data) if data else None


async def _store(key: str, result: ReferenceResult, config: LibraryConfig) -> None:
    try:
        await cache.put_json(key, result.model_dump(), config.limits.cache_ttl_hours * 3600)
    except Exception:
        log.debug("library cache unavailable", exc_info=True)


async def check_literature(
    content: ExtractedContent, config: LibraryConfig | None = None
) -> LibraryReport | None:
    """None when the lookup is switched off. A title that could not be searched is reported as `error`,
    and one past `max_references` as `not_checked`; neither stops the others."""
    config = config or get_library_config()
    if config.provider == "off":
        return None
    refs = split_references(literature_lines(content))
    results: list[ReferenceResult] = []
    async with megapro.client(config) as opac:
        for i, ref in enumerate(refs):
            if i >= config.lookup.max_references:
                results.append(ReferenceResult(text=ref.text, kind=ref.kind, status="not_checked"))
                continue
            key = _cache_key(ref, config)
            if (hit := await _cached(key)) is not None:
                results.append(hit)
                continue
            try:
                found = await _find(ref, opac, config)
            except megapro.LibraryUnavailable as exc:
                log.warning("library lookup failed for %r: %s", ref.text[:80], exc)
                results.append(ReferenceResult(text=ref.text, kind=ref.kind, status="error", error=str(exc)))
                continue
            await _store(key, found, config)
            results.append(found)
    return LibraryReport(provider=config.provider, references=results)


def _short(text: str, limit: int = 110) -> str:
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def criterion_result(
    spec: CriterionSpec, report: LibraryReport, config: LibraryConfig | None = None
) -> CriterionResultData:
    """The status follows integrations/library.yaml's `status` thresholds; nothing here is left to a model."""
    config = config or get_library_config()

    def result(
        status: ResultStatus, evidence: str, explanation: str, recommendation: str
    ) -> CriterionResultData:
        return CriterionResultData(
            criterion_id=spec.code,
            status=status,
            evidence=evidence,
            explanation=explanation,
            recommendation=recommendation,
            required_context=spec.required_context,
            confidence=1.0 if status != ResultStatus.cannot_verify else None,
        )

    considered = report.considered()
    if not considered:
        # Not "Not compliant": a list the extractor could not find is not a list that is missing. Whether the
        # syllabus has literature at all is judged by the other Resources criteria.
        return result(
            ResultStatus.cannot_verify,
            "",
            "Cannot verify this criterion because no literature list could be read from the syllabus.",
            "Give the literature its own section (Основная литература / Core literature) so each title can be "
            "looked up in the Narxoz Scientific Library.",
        )
    checked = [r for r in considered if r.status in ("held", "no_copies", "not_found")]
    if not checked:
        why = next((r.error for r in considered if r.error), "the catalogue did not answer")
        return result(
            ResultStatus.cannot_verify,
            "",
            f"Cannot verify this criterion because the library catalogue could not be searched ({why}).",
            "Run this criterion again when the library catalogue is reachable.",
        )
    held = [r for r in checked if r.status == "held"]
    missing = [r for r in checked if r.status != "held"]
    share = len(held) / len(checked)
    if share >= config.status.compliant_at:
        status = ResultStatus.compliant
    elif share >= config.status.partial_at:
        status = ResultStatus.partial
    else:
        status = ResultStatus.not_compliant

    def held_line(r: ReferenceResult) -> str:
        where = f"{r.copies} cop{'y' if r.copies == 1 else 'ies'}" if r.copies else "electronic version"
        return f"- Held ({where}): {_short(r.text)}"

    lines = [held_line(r) for r in held]
    lines += [
        f"- {'In the catalogue, no copies' if r.status == 'no_copies' else 'Not found'}: {_short(r.text)}"
        for r in missing
    ]
    unchecked = [r for r in considered if r not in checked]
    scope = "core titles" if any(r.kind == "core" for r in report.references) else "titles"
    explanation = (
        f"{len(held)} of {len(checked)} {scope} are held by the Narxoz Scientific Library.\n"
        + "\n".join(lines)
    )
    if unchecked:
        explanation += f"\n{len(unchecked)} more could not be checked."
    recommendation = (
        "No change needed."
        if not missing
        else "Replace the titles the library does not hold with ones it does, or ask the library to acquire them: "
        + "; ".join(_short(r.text, 80) for r in missing[:5])
        + ("; …" if len(missing) > 5 else "")
    )
    evidence = "\n".join(r.text for r in checked[:3])
    return result(status, evidence, explanation, recommendation)
