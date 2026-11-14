"""The analysis page and its report: what the AI checks found across a semester, by severity.

The page is aggregated at every scope: across the university it lists Schools, within a School the courses
that need attention most. The report adds each course's findings, and is limited in size, because a PDF of
hundreds of courses is read by nobody: a larger scope is narrowed to a School, or to critical findings."""

import uuid
from collections import Counter, defaultdict
from datetime import UTC, datetime
from typing import Any, Literal

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.rbac import Principal
from app.documents import AssessmentResultDoc
from app.enums import ISSUE_STATUSES, AiStatus, ResultStatus, RunStatus, Severity, severity
from app.models import AssessmentRun, Criterion, CriterionOutcome, School, Syllabus, SyllabusVersion
from app.services import analytics
from app.services.rubric import LoadedRubric, load_rubric
from app.services.syllabi import syllabus_rows_query

# The most courses one report holds. Beyond it the report is refused with a way to narrow it.
REPORT_LIMIT = 60
# How many courses the page lists as needing attention most, within a School.
ATTENTION = 10

Bucket = Literal["critical", "major", "minor", "clean", "cannot_check", "not_checked"]


def _bucket(s: Syllabus) -> Bucket:
    """Where a syllabus counts: by its worst finding, or why it has no result."""
    if s.ai_status == AiStatus.cannot_check:
        return "cannot_check"
    if s.ai_status not in (AiStatus.checked, AiStatus.needs_revision) or s.issues is None:
        return "not_checked"
    if not s.issues:
        return "clean"
    if s.critical_issues:
        return "critical"
    if s.major_issues:
        return "major"
    return "minor"


def _counts(syllabi: list[Syllabus]) -> dict[str, int]:
    c = Counter(_bucket(s) for s in syllabi)
    return {
        "found": len(syllabi),
        "checked": c["critical"] + c["major"] + c["minor"] + c["clean"],
        "with_critical": c["critical"],
        "major_only": c["major"],
        "minor_only": c["minor"],
        "clean": c["clean"],
        "cannot_check": c["cannot_check"],
        "not_checked": c["not_checked"],
    }


def _severities(s: Syllabus) -> dict[str, int]:
    critical, major = s.critical_issues or 0, s.major_issues or 0
    return {"critical": critical, "major": major, "minor": max(0, (s.issues or 0) - critical - major)}


async def _rows(session: AsyncSession, principal: Principal, semester: str, school_id: uuid.UUID | None):
    query = syllabus_rows_query(principal).where(
        Syllabus.semester == semester, Syllabus.drive_missing_at.is_(None)
    )
    if school_id:
        query = query.where(School.id == school_id)
    return (await session.execute(query)).all()


async def _top_issue(
    session: AsyncSession, principal: Principal, semester: str, school_id: uuid.UUID | None, per: str
) -> dict[uuid.UUID, dict]:
    """For each School (per="school") or syllabus (per="syllabus"), the finding that matters most: the
    critical criterion failed most often, else the criterion failed most often."""
    latest = analytics._latest_runs(principal, semester, school_id, None)
    key = latest.c.school_id if per == "school" else latest.c.syllabus_id
    rows = (
        await session.execute(
            sa.select(key, Criterion.code, Criterion.name, Criterion.critical, sa.func.count())
            .join(CriterionOutcome, CriterionOutcome.run_id == latest.c.run_id)
            .join(Criterion, Criterion.id == CriterionOutcome.criterion_id)
            .where(CriterionOutcome.status.in_(ISSUE_STATUSES))
            .group_by(key, Criterion.code, Criterion.name, Criterion.critical)
        )
    ).all()
    best: dict[uuid.UUID, tuple] = {}
    for owner, code, name, critical, n in rows:
        rank = (critical, n, -_order(code))
        if owner not in best or rank > best[owner][0]:
            best[owner] = (rank, {"criterion_id": code, "name": name, "critical": critical})
    return {owner: v[1] for owner, v in best.items()}


def _order(code: str) -> float:
    """ "6.10" after "6.9": criterion codes sort as numbers."""
    principle, _, position = code.partition(".")
    return int(principle) * 1000 + int(position or 0)


async def summary(
    session: AsyncSession, principal: Principal, *, semester: str, school_id: uuid.UUID | None = None
) -> dict:
    rows = await _rows(session, principal, semester, school_id)
    by_school: dict[uuid.UUID, list] = defaultdict(list)
    names: dict[uuid.UUID, str] = {}
    for r in rows:
        by_school[r.school_id].append(r.Syllabus)
        names[r.school_id] = r.school_name
    issues = await analytics.frequent_issues(
        session, principal, semester=semester, school_id=school_id, limit=100
    )
    top_by_school = await _top_issue(session, principal, semester, school_id, "school")

    attention: list[dict] = []
    if school_id:
        worst = sorted(
            (r for r in rows if _bucket(r.Syllabus) in ("critical", "major", "minor")),
            key=lambda r: (
                -(r.Syllabus.critical_issues or 0),
                -(r.Syllabus.major_issues or 0),
                r.course_name,
            ),
        )[:ATTENTION]
        tops = await _top_issue(session, principal, semester, school_id, "syllabus") if worst else {}
        attention = [
            {
                "syllabus_id": r.Syllabus.id,
                "course_name": r.course_name,
                "professor_name": r.professor_name,
                "language": r.Syllabus.language,
                **_severities(r.Syllabus),
                "top_issue": tops.get(r.Syllabus.id),
            }
            for r in worst
        ]
    return {
        "semester": semester,
        "school_id": school_id,
        "school_name": names.get(school_id) if school_id else None,
        "totals": _counts([r.Syllabus for r in rows]),
        "schools": [
            {
                "school_id": sid,
                "school_name": names[sid],
                **_counts(syllabi),
                "top_issue": top_by_school.get(sid),
            }
            for sid, syllabi in sorted(by_school.items(), key=lambda kv: names[kv[0]])
        ],
        "issues": issues,
        "attention": attention,
        "report_limit": REPORT_LIMIT,
    }


class ReportTooLarge(Exception):
    pass


async def report(
    session: AsyncSession,
    principal: Principal,
    *,
    semester: str,
    school_id: uuid.UUID | None,
    include: Literal["summary", "critical", "all"],
) -> dict:
    head = await summary(session, principal, semester=semester, school_id=school_id)
    head.pop("attention")
    rows = await _rows(session, principal, semester, school_id)
    wanted = {"summary": (), "critical": ("critical",), "all": ("critical", "major", "minor", "clean")}[
        include
    ]
    picked = [r for r in rows if _bucket(r.Syllabus) in wanted]
    if len(picked) > REPORT_LIMIT:
        raise ReportTooLarge(
            f"The report would hold {len(picked)} courses; it holds at most {REPORT_LIMIT}. "
            "Pick one School, or only the courses with critical findings."
        )
    picked.sort(
        key=lambda r: (
            r.school_name,
            -(r.Syllabus.critical_issues or 0),
            -(r.Syllabus.major_issues or 0),
            r.course_name,
        )
    )
    courses: list[dict] = []
    if picked:
        ids = [r.Syllabus.id for r in picked]
        runs = {
            sid: (run_id, rubric_id)
            for sid, run_id, rubric_id in (
                await session.execute(
                    sa.select(Syllabus.id, AssessmentRun.id, AssessmentRun.rubric_version_id)
                    .join(
                        SyllabusVersion,
                        sa.and_(
                            SyllabusVersion.syllabus_id == Syllabus.id,
                            SyllabusVersion.version == Syllabus.current_version,
                        ),
                    )
                    .join(AssessmentRun, AssessmentRun.version_id == SyllabusVersion.id)
                    .where(Syllabus.id.in_(ids), AssessmentRun.status == RunStatus.completed)
                    .order_by(Syllabus.id, AssessmentRun.completed_at.desc())
                    .distinct(Syllabus.id)
                )
            ).all()
        }
        docs = {
            d.run_id: d
            for d in await AssessmentResultDoc.find(
                {"run_id": {"$in": [run_id for run_id, _ in runs.values()]}}
            ).to_list()
        }
        rubrics: dict[uuid.UUID, LoadedRubric] = {}
        for _, rubric_id in runs.values():
            if rubric_id not in rubrics:
                rubrics[rubric_id] = await load_rubric(session, rubric_id)
        rank = {Severity.critical: 0, Severity.major: 1, Severity.minor: 2}
        for r in picked:
            run = runs.get(r.Syllabus.id)
            doc = docs.get(run[0]) if run else None
            findings: list[dict[str, Any]] = []
            if doc and run:
                loaded = rubrics[run[1]]
                specs = {s.code: s for s in loaded.specs}
                for res in doc.results:
                    spec = specs.get(res.criterion_id)
                    sev = severity(res.status, bool(spec and spec.critical))
                    if sev is None or spec is None:
                        continue
                    findings.append(
                        {
                            "criterion_id": res.criterion_id,
                            "name": spec.name,
                            "principle": spec.principle,
                            "principle_name": spec.principle_name,
                            "severity": sev,
                            "status": ResultStatus(res.status),
                            "evidence": res.evidence,
                            "explanation": res.explanation,
                            "recommendation": res.recommendation,
                        }
                    )
                findings.sort(key=lambda f: (rank[f["severity"]], _order(f["criterion_id"])))
            courses.append(
                {
                    "syllabus_id": r.Syllabus.id,
                    "course_name": r.course_name,
                    "school_name": r.school_name,
                    "program_name": r.program_name,
                    "professor_name": r.professor_name,
                    "language": r.Syllabus.language,
                    **_severities(r.Syllabus),
                    "findings": findings,
                }
            )
    return {**head, "include": include, "generated_at": datetime.now(UTC), "courses": courses}
