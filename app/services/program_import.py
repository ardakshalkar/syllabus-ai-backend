"""Import the educational programs, their learning outcomes and disciplines from narxoz.edu.kz.

The public site is backed by a JSON API (narxoz.kz/api/v1/education/programs/) that answers in Russian, Kazakh or
English depending on Accept-Language. Each outcome and discipline keeps the same id across the three languages, so
the languages are paired by id, not by position.

Programs are matched to ours by their code (6B04103). Outcome rows are updated in place by number, so their ids stay
stable for anything that refers to them later (alignment of syllabus outcomes to program outcomes).
"""

import asyncio
import html
import logging
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime

import httpx
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Program, ProgramDiscipline, ProgramOutcome, School

log = logging.getLogger(__name__)

API = "https://narxoz.kz/api/v1/education/programs/"
PAGE = "https://narxoz.edu.kz/program/{slug}"
LANGS = ("ru", "kk", "en")

# The site's School slugs, mapped to ours (app.services.demo_users.SCHOOLS).
SCHOOL_BY_SITE = {
    "economics": "economics-management",
    "school-of-law-and-public-policy": "law-public-administration",
    "school-of-digital-technologies": "digital-technologies",
    # "Гуманитарная школа" on the site: sociology, psychology, social work, ecology, tourism.
    "school-of-arts-and-social-sciences": "shspn",
}
LEVELS = {"bachelor": "Bachelor", "master": "Master", "doctorate": "Doctorate"}

# Codes on the site are typed with Cyrillic look-alikes about as often as with Latin letters (6В06301, 7М04124).
_LATIN = str.maketrans("АВЕКМНОРСТХДавекмнорстхд", "ABEKMHOPCTXDABEKMHOPCTXD")
_CODE = re.compile(r"^[678][BMD]\d{5}$")
_TAG = re.compile(r"<[^>]+>")
_SPACE = re.compile(r"\s+")


def normalize_code(code: str | None) -> str:
    return re.sub(r"\s+", "", (code or "").translate(_LATIN)).upper()


def html_text(value: str | None) -> str:
    """Rich-text HTML from the site's CMS as one plain line ("<p>Готовит&nbsp;отчётность</p>" -> "Готовит отчётность")."""
    text = html.unescape(_TAG.sub(" ", value or ""))
    return _SPACE.sub(" ", text.replace("\xa0", " ")).strip()


def field_of(code: str) -> str:
    """The field-of-study group in a code (6B04103 -> 041): programs sharing it belong to the same School."""
    return code[2:5]


@dataclass
class SiteProgram:
    code: str
    site_slug: str
    site_school: str  # the site's School slug; "" when the site gives none
    degree_level: str
    titles: dict[str, str]
    goal: dict[str, str]
    outcomes: list[dict[str, str]]  # in order; each {"ru": ..., "kk": ..., "en": ...}
    disciplines: list[tuple[dict[str, str], bool]]  # (names, is a profile subject)


def _by_language(details: dict[str, dict], key: str, text_key: str) -> list[tuple[dict[str, str], dict]]:
    """Items of a list field, in the Russian order, with their text in every language that has one."""
    texts: dict[int, dict[str, str]] = {}
    for lang in LANGS:
        for item in (details.get(lang) or {}).get(key) or []:
            if value := html_text(item.get(text_key)):
                texts.setdefault(item["id"], {})[lang] = value
    base = details.get("ru") or next(d for d in details.values() if d)
    ordered = sorted(base.get(key) or [], key=lambda i: (i.get("sort_order") or 0, i["id"]))
    return [(texts[i["id"]], i) for i in ordered if i["id"] in texts]


def parse_program(details: dict[str, dict]) -> SiteProgram:
    """One program from its detail responses, keyed by language ("ru" is required)."""
    ru = details["ru"]
    titles = {lang: t for lang in LANGS if (t := html_text((details.get(lang) or {}).get("title")))}
    goal = {lang: t for lang in LANGS if (t := html_text((details.get(lang) or {}).get("goal")))}
    levels = [lvl["slug"] for lvl in ru.get("levels") or []]
    seen: set[str] = set()
    disciplines = []
    for names, item in _by_language(details, "disciplines", "name"):
        # The site lists some courses twice; one entry is enough to match a syllabus to the program.
        if (key := names.get("ru", "").casefold()) not in seen:
            seen.add(key)
            disciplines.append((names, bool(item.get("is_profile_subject"))))
    return SiteProgram(
        code=normalize_code(ru.get("code")),
        site_slug=ru["slug"],
        site_school=ru.get("school_slug") or "",
        degree_level=LEVELS.get(levels[0], levels[0].title()) if levels else "Not set",
        titles=titles,
        goal=goal,
        outcomes=[texts for texts, _ in _by_language(details, "learning_outcomes", "text")],
        disciplines=disciplines,
    )


async def _get(client: httpx.AsyncClient, url: str, lang: str = "ru") -> dict:
    response = await client.get(url, headers={"Accept-Language": lang})
    response.raise_for_status()
    return response.json()


async def fetch_site_programs(
    client: httpx.AsyncClient, codes: set[str] | None = None
) -> tuple[list[SiteProgram], list[tuple[str, str]]]:
    """Every program on the site (or only those with these codes), and (slug, error) for those that failed."""
    listed: list[dict] = []
    url: str | None = API
    while url:
        page = await _get(client, url)
        listed += page["results"]
        url = page.get("next")
    if codes:
        listed = [p for p in listed if normalize_code(p.get("code")) in codes]

    gate = asyncio.Semaphore(4)  # be gentle with the university's site

    async def one(slug: str) -> SiteProgram:
        async with gate:
            details = {lang: await _get(client, f"{API}{slug}/", lang) for lang in LANGS}
        return parse_program(details)

    results = await asyncio.gather(*(one(p["slug"]) for p in listed), return_exceptions=True)
    programs, failed = [], []
    for p, result in zip(listed, results, strict=True):
        if isinstance(result, BaseException):
            log.warning("program %s: %s", p["slug"], result)
            failed.append((p["slug"], str(result) or type(result).__name__))
        else:
            programs.append(result)
    return programs, failed


async def fetch_site_program(client: httpx.AsyncClient, slug: str) -> SiteProgram:
    """One program from the site by its page slug (what a restore reads, instead of listing every program)."""
    details = {lang: await _get(client, f"{API}{slug}/", lang) for lang in LANGS}
    return parse_program(details)


def replace_outcomes(program: Program, outcomes: list[dict[str, str]]) -> None:
    """The program's outcomes become these: rows keep their ids by number, new ones are added, the surplus goes."""
    _sync_rows(program.outcomes, outcomes, lambda n, t: ProgramOutcome(number=n, text=t), _set_outcome)


@dataclass
class ImportReport:
    created: list[str] = field(default_factory=list)
    updated: list[str] = field(default_factory=list)
    skipped: list[tuple[str, str]] = field(default_factory=list)  # (code or slug, reason)
    school_inferred: list[tuple[str, str]] = field(default_factory=list)  # (code, School slug)
    outcomes_kept: list[str] = field(
        default_factory=list
    )  # codes whose outcomes were edited by hand: not touched
    outcomes: int = 0
    disciplines: int = 0


def _sync_rows(rows: list, wanted: list, make, fill) -> None:
    """Update rows in place by position (row.number), append new ones, drop the surplus: ids stay stable."""
    by_number = {r.number: r for r in rows}
    for number, value in enumerate(wanted, start=1):
        if (row := by_number.pop(number, None)) is None:
            rows.append(make(number, value))
        else:
            fill(row, value)
    for row in by_number.values():
        rows.remove(row)


def _set_outcome(row: ProgramOutcome, texts: dict[str, str]) -> None:
    row.text = texts


def _sync_disciplines(program: Program, wanted: list[tuple[dict[str, str], bool]]) -> None:
    """Refresh the courses read from the site, in the site's order, matching them by position. A course someone
    edited keeps its text (and is not dropped if the site no longer lists it); one added by hand is not touched.
    Edited and manual courses go after the site's own, so the numbers stay 1..n."""
    site = sorted((d for d in program.disciplines if d.source == "site"), key=lambda d: d.number)
    for position, row in enumerate(site):
        if position >= len(wanted):
            if not row.edited:
                program.disciplines.remove(row)
        elif not row.edited:
            row.name, row.is_profile = wanted[position]
    for names, is_profile in wanted[len(site) :]:
        program.disciplines.append(ProgramDiscipline(number=0, name=names, is_profile=is_profile))
    kept = [d for d in program.disciplines if d.source == "site"]
    manual = sorted((d for d in program.disciplines if d.source != "site"), key=lambda d: d.number)
    # New rows have number 0 and sort first among themselves only by insertion: keep that order by index.
    ordered = sorted(kept, key=lambda d: (d.number == 0, d.number)) + manual
    for number, row in enumerate(ordered, start=1):
        row.number = number


async def import_programs(
    session: AsyncSession, site: list[SiteProgram], now: datetime | None = None
) -> ImportReport:
    now = now or datetime.now(UTC)
    report = ImportReport()
    schools = {s.slug: s for s in await session.scalars(sa.select(School))}

    # The site leaves a few programs (doctorates) without a School; take it from programs in the same field.
    field_school: dict[str, str] = {}
    for p in site:
        if (ours := SCHOOL_BY_SITE.get(p.site_school)) and ours in schools:
            field_school.setdefault(field_of(p.code), ours)

    existing = {p.code: p for p in await session.scalars(sa.select(Program).where(Program.code.is_not(None)))}
    for p in site:
        if not _CODE.match(p.code):
            report.skipped.append((p.site_slug, f"unrecognised program code {p.code!r}"))
            continue
        if not p.outcomes:
            report.skipped.append((p.code, "the site lists no learning outcomes"))
            continue
        school_slug = SCHOOL_BY_SITE.get(p.site_school)
        if school_slug is None and not p.site_school and (school_slug := field_school.get(field_of(p.code))):
            report.school_inferred.append((p.code, school_slug))
        school = schools.get(school_slug or "")
        if school is None:
            reason = (
                f"site School {p.site_school!r} has no match here"
                if p.site_school
                else "the site gives no School"
            )
            report.skipped.append((p.code, f"{reason} (run setup-schools first?)"))
            continue

        program = existing.get(p.code)
        if program is None:
            program = Program(code=p.code, slug=p.code.lower(), outcomes=[], disciplines=[])
            session.add(program)
            existing[p.code] = program
            report.created.append(p.code)
        else:
            report.updated.append(p.code)
        if program.details_edited_at is None:  # a corrected name and level stay corrected
            program.name = (p.titles.get("ru") or p.titles.get("en") or p.code)[:200]
            program.degree_level = p.degree_level[:40]
        program.school_id = school.id
        program.site_slug = p.site_slug
        program.titles = p.titles
        program.goal = p.goal or None
        program.imported_at = now
        if program.outcomes_edited_at is None:
            replace_outcomes(program, p.outcomes)
            report.outcomes += len(p.outcomes)
        else:
            report.outcomes_kept.append(p.code)
        _sync_disciplines(program, p.disciplines)
        report.disciplines += len(p.disciplines)
    await session.flush()
    return report


def page_url(site_slug: str) -> str:
    return PAGE.format(slug=site_slug)
