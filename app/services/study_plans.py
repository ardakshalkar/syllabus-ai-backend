"""Learning plans (учебные планы): import from the advisor's export, edit, and find the plan course a syllabus is.

A syllabus is matched to a plan course by, in this order: its course code (the plan's own, or a Platonus code),
its title in any language exactly, its title spelled closely. Credits then confirm or doubt the match. A match is
a suggestion: the School confirms a course's program (services/program_links.set_manual).
"""

from __future__ import annotations

import difflib
import re
import uuid
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import PlanSource, PlanStatus, Program, StudyPlan, StudyPlanCourse, StudyPlanEvent
from app.services.program_import import normalize_code
from app.services.program_links import normalize_name

FUZZY = 0.86
_LOOKALIKE = str.maketrans("асекорхву", "acekopxby")  # Cyrillic letters that look Latin, in course codes


def code_key(code: str | None) -> str:
    """A course code compared loosely: no spaces or dashes, any case, Cyrillic look-alikes made Latin."""
    return re.sub(r"[\W_]+", "", (code or "").casefold().translate(_LOOKALIKE))


# ---------------------------------------------------------------- reading a syllabus header

_HEADER_KEY = re.compile(
    r"(код.*(атау|назван)|шифр.*назв|course\s*code|code and (full )?(title|name))", re.IGNORECASE
)
_COURSE_CODE = re.compile(r"\b([A-Za-zА-Яа-яЁёӘәІіҢңҒғҮүҰұҚқӨөҺһ]{2,9})\s?-?\s?(\d{3,5})\b")
_LANG_TAG = re.compile(r"\b(ENG|KAZ|RUS|КАЗ|РУС)\s?-?\s?\d?\s*(\(\d\))?[\s\-:]*", re.IGNORECASE)
_PROGRAM_CODE = re.compile(r"\b[6-8][BbВвDdMmМм]\d{5}\b")
_MARKUP = re.compile("<[^>]+>|[*]|" + re.escape(chr(92)))


@dataclass
class SyllabusHeader:
    code: str | None = None
    title: str | None = None
    credits: float | None = None
    program_codes: list[str] = field(default_factory=list)


def syllabus_header(text: str | None) -> SyllabusHeader | None:
    """Course code, title, credits and programme codes from the syllabus's first table
    ("Programme | Code and title of the discipline | Semester | Credits"). None when there is no such table."""
    lines = [ln for ln in (text or "").split("\n") if ln.strip().startswith("|")]
    for i, line in enumerate(lines):
        if not _HEADER_KEY.search(line):
            continue
        header = [c.strip(" *") for c in line.strip().strip("|").split("|")]
        j = i + 1
        while j < len(lines) and re.fullmatch(r"[\s|:\-]+", lines[j]):
            j += 1
        if j >= len(lines):
            return None
        row = [_MARKUP.sub("", c).strip() for c in lines[j].strip().strip("|").split("|")]
        at = next((k for k, c in enumerate(header) if _HEADER_KEY.search(c)), None)
        cell = row[at] if at is not None and at < len(row) else ""
        found = _COURSE_CODE.search(cell)
        code = (found.group(1) + found.group(2)) if found else None
        title = cell.replace(found.group(0), " ", 1) if found else cell
        title = _LANG_TAG.sub(" ", title)
        title = re.sub(r"^[\s\-–—:/«»\"]+|[\s\-–—:/«»\"]+$", "", title)
        credits = None
        for k, c in enumerate(header):
            if re.search(r"кредит|credit", c, re.IGNORECASE) and k < len(row):
                number = re.search(r"\d+(?:[.,]\d+)?", row[k])
                if number:
                    credits = float(number.group().replace(",", "."))
                    break
        programmes = [normalize_code(m) for m in _PROGRAM_CODE.findall(row[0] if row else "")]
        return SyllabusHeader(code, title or None, credits, programmes)
    return None


# ---------------------------------------------------------------- matching


@dataclass
class PlanMatch:
    plan: StudyPlan
    course: StudyPlanCourse
    how: str  # "code" | "title" | "close title"
    credits_agree: bool | None  # None when either side has no credits


class PlanIndex:
    """Every course of the active plans, by code and by title in any language."""

    def __init__(self, plans: list[StudyPlan]):
        self.by_code: dict[str, list[tuple[StudyPlan, StudyPlanCourse]]] = defaultdict(list)
        self.by_title: dict[str, list[tuple[StudyPlan, StudyPlanCourse]]] = defaultdict(list)
        for plan in plans:
            for course in plan.courses:
                codes = {code_key(course.code), *(code_key(c) for c in course.platonus_codes or [])}
                for key in codes - {""}:
                    self.by_code[key].append((plan, course))
                for name in {course.title, *(course.names or {}).values()}:
                    if key := normalize_name(name):
                        self.by_title[key].append((plan, course))
        self._titles = list(self.by_title)

    @classmethod
    async def load(cls, session: AsyncSession) -> PlanIndex:
        plans = await session.scalars(sa.select(StudyPlan).where(StudyPlan.status == PlanStatus.active))
        return cls(list(plans.unique()))

    def match(
        self, *, code: str | None = None, title: str | None = None, credits: float | None = None
    ) -> list[PlanMatch]:
        """Plan courses this syllabus is, by the first way that finds any; all plans (every program and year)."""
        hits: list[tuple[StudyPlan, StudyPlanCourse]] = []
        how = ""
        if code and code_key(code) in self.by_code:
            hits, how = self.by_code[code_key(code)], "code"
        if not hits and (key := normalize_name(title)):
            if key in self.by_title:
                hits, how = self.by_title[key], "title"
            elif close := difflib.get_close_matches(key, self._titles, n=1, cutoff=FUZZY):
                hits, how = self.by_title[close[0]], "close title"
        return [
            PlanMatch(
                plan,
                course,
                how,
                None if credits is None or course.credits is None else abs(course.credits - credits) < 0.01,
            )
            for plan, course in hits
        ]


def programs_of(matches: list[PlanMatch]) -> dict[str, PlanMatch]:
    """The best match per program: the latest intake year, and a credit-confirmed one over an unconfirmed one."""
    best: dict[str, PlanMatch] = {}
    for m in matches:
        cur = best.get(m.plan.program_code)
        rank = (m.credits_agree is True, m.plan.intake_year or 0)
        if cur is None or rank > (cur.credits_agree is True, cur.plan.intake_year or 0):
            best[m.plan.program_code] = m
    return best


# ---------------------------------------------------------------- writing


def _course(row: dict[str, Any], position: int) -> StudyPlanCourse:
    names = {k: v for k, v in (row.get("names") or {}).items() if v}
    title = row.get("title") or names.get("en") or names.get("ru") or names.get("kk")
    return StudyPlanCourse(
        position=position,
        code=(row.get("code") or None),
        platonus_codes=list(row.get("platonus_codes") or []),
        title=title,
        names=names,
        credits=row.get("credits"),
        year=row.get("year"),
        term=row.get("term"),
        elective_group=row.get("elective_group"),
    )


def _fingerprint(courses: list[StudyPlanCourse] | list[dict[str, Any]]) -> list[tuple]:
    rows = [
        (c.code, c.title, c.credits, c.year, c.term, c.elective_group)
        if isinstance(c, StudyPlanCourse)
        else (
            c.get("code") or None,
            c.get("title"),
            c.get("credits"),
            c.get("year"),
            c.get("term"),
            c.get("elective_group"),
        )
        for c in courses
    ]
    return sorted(rows, key=lambda r: tuple("" if v is None else str(v) for v in r))


async def log(
    session: AsyncSession, plan: StudyPlan, user_id: uuid.UUID | None, action: str, summary: str = ""
) -> None:
    session.add(StudyPlanEvent(plan_id=plan.id, user_id=user_id, action=action, summary=summary))


async def program_id_for(session: AsyncSession, code: str) -> uuid.UUID | None:
    return await session.scalar(sa.select(Program.id).where(Program.code == normalize_code(code)))


async def set_courses(session: AsyncSession, plan: StudyPlan, rows: list[dict[str, Any]]) -> None:
    """Replace every course of the plan. Done with SQL, because assigning the relationship would load the old
    collection, which an async session cannot do lazily."""
    session.add(plan)
    await session.flush()
    await session.execute(sa.delete(StudyPlanCourse).where(StudyPlanCourse.plan_id == plan.id))
    for position, row in enumerate(rows):
        course = _course(row, position)
        course.plan_id = plan.id
        session.add(course)
    await session.flush()
    await session.refresh(plan, ["courses"])


async def import_export(
    session: AsyncSession, data: dict[str, Any], *, user_id: uuid.UUID | None = None, dry_run: bool = False
) -> dict[str, Any]:
    """Load the advisor's export (scripts/export_study_plans.py). Idempotent: a plan is found again by its Platonus
    curriculum id, and left alone if it is unchanged or if someone edited it by hand since the last import."""
    report: dict[str, Any] = {
        "created": 0,
        "updated": 0,
        "unchanged": 0,
        "skipped_edited": [],
        "dry_run": dry_run,
    }
    seen: set[str] = set()
    for i, item in enumerate(data.get("plans", [])):
        code = normalize_code(item.get("program_code"))
        if not code or not item.get("courses"):
            continue
        pid = item.get("platonus_curriculum_id")
        key = (
            f"platonus:{pid}"
            if pid
            else f"advisor:{code}:{item.get('intake_year')}:{item.get('variant') or ''}:{i}"
        )
        if key in seen:
            continue
        seen.add(key)
        plan = await session.scalar(sa.select(StudyPlan).where(StudyPlan.external_key == key))
        label = f"{code} {item.get('intake_year') or ''} {item.get('variant') or ''}".strip()
        if plan is None:
            report["created"] += 1
            if dry_run:
                continue
            plan = StudyPlan(
                external_key=key, program_code=code, status=PlanStatus.active, source=PlanSource.advisor
            )
            session.add(plan)
        else:
            if plan.edited:
                report["skipped_edited"].append(label)
                continue
            same_details = (plan.total_credits, plan.variant, plan.program_name, plan.intake_year) == (
                item.get("total_credits"),
                item.get("variant"),
                item.get("program_name"),
                item.get("intake_year"),
            )
            if same_details and _fingerprint(plan.courses) == _fingerprint(item["courses"]):
                report["unchanged"] += 1
                continue
            report["updated"] += 1
            if dry_run:
                continue
            plan.version += 1
        plan.program_id = await program_id_for(session, code)
        plan.program_name = item.get("program_name")
        plan.intake_year = item.get("intake_year")
        plan.variant = item.get("variant")
        plan.total_credits = item.get("total_credits")
        plan.platonus_curriculum_id = pid
        plan.source_file = item.get("source_file")
        await set_courses(session, plan, item["courses"])
        await log(session, plan, user_id, "import", f"{len(plan.courses)} courses")
    if not dry_run:
        await session.flush()
    report["at"] = datetime.now(UTC).isoformat(timespec="seconds")
    return report
