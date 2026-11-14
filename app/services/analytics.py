"""Overview cards, heatmap, frequent issues and CSV export. Plain SQL over criterion_outcomes."""

import csv
import io
import uuid

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.rbac import Principal, syllabus_filter
from app.enums import ISSUE_STATUSES, AiStatus, ApprovalStage, ResultStatus, RunStatus
from app.models import (
    AssessmentRun,
    Course,
    Criterion,
    CriterionOutcome,
    Principle,
    Program,
    RubricVersion,
    School,
    Syllabus,
    SyllabusVersion,
)


def _latest_runs(
    principal: Principal, semester: str, school_id: uuid.UUID | None, program_id: uuid.UUID | None
):
    """Latest completed run of each visible syllabus's current version."""
    query = (
        sa.select(
            AssessmentRun.id.label("run_id"),
            Syllabus.id.label("syllabus_id"),
            Program.school_id.label("school_id"),
        )
        .join(SyllabusVersion, SyllabusVersion.id == AssessmentRun.version_id)
        .join(
            Syllabus,
            sa.and_(
                Syllabus.id == SyllabusVersion.syllabus_id,
                Syllabus.current_version == SyllabusVersion.version,
            ),
        )
        .join(Course, Course.id == Syllabus.course_id)
        .join(Program, Program.id == Course.program_id)
        .where(
            AssessmentRun.status == RunStatus.completed,
            Syllabus.semester == semester,
            syllabus_filter(principal),
        )
        .distinct(Syllabus.id)
        .order_by(Syllabus.id, AssessmentRun.completed_at.desc())
    )
    if school_id:
        query = query.where(Program.school_id == school_id)
    if program_id:
        query = query.where(Program.id == program_id)
    return query.subquery("latest")


async def heatmap(
    session: AsyncSession, principal: Principal, *, semester: str, program_id: uuid.UUID | None = None
) -> dict:
    latest = _latest_runs(principal, semester, None, program_id)
    totals: dict[uuid.UUID, int] = dict(
        (
            await session.execute(sa.select(latest.c.school_id, sa.func.count()).group_by(latest.c.school_id))
        ).all()
    )
    flagged = (
        await session.execute(
            sa.select(latest.c.school_id, Principle.number, sa.func.count(sa.distinct(latest.c.syllabus_id)))
            .join(CriterionOutcome, CriterionOutcome.run_id == latest.c.run_id)
            .join(Criterion, Criterion.id == CriterionOutcome.criterion_id)
            .join(Principle, Principle.id == Criterion.principle_id)
            .where(CriterionOutcome.status.in_(ISSUE_STATUSES))
            .group_by(latest.c.school_id, Principle.number)
        )
    ).all()
    principles = (
        await session.execute(
            sa.select(Principle.number, Principle.name)
            .join(RubricVersion, RubricVersion.id == Principle.rubric_version_id)
            .where(RubricVersion.is_active.is_(True))
            .order_by(Principle.number)
        )
    ).all()
    schools = (await session.execute(sa.select(School.id, School.name).where(School.id.in_(totals)))).all()
    counts = {(s, n): c for s, n, c in flagged}
    return {
        "semester": semester,
        "principles": [{"number": n, "name": name} for n, name in principles],
        "rows": [
            {
                "school_id": sid,
                "school_name": name,
                "syllabi_checked": totals[sid],
                "cells": [
                    {
                        "principle": n,
                        "flagged": counts.get((sid, n), 0),
                        "percent": round(100 * counts.get((sid, n), 0) / totals[sid]),
                    }
                    for n, _ in principles
                ],
            }
            for sid, name in sorted(schools, key=lambda r: r[1])
        ],
    }


async def frequent_issues(
    session: AsyncSession,
    principal: Principal,
    *,
    semester: str,
    school_id: uuid.UUID | None = None,
    program_id: uuid.UUID | None = None,
    limit: int = 5,
) -> list[dict]:
    latest = _latest_runs(principal, semester, school_id, program_id)
    total = await session.scalar(sa.select(sa.func.count()).select_from(latest)) or 0
    rows = (
        await session.execute(
            sa.select(
                Criterion.code,
                Criterion.name,
                Criterion.critical,
                Principle.number,
                Principle.name,
                sa.func.count(sa.distinct(latest.c.syllabus_id)).label("n"),
                sa.func.count(sa.distinct(latest.c.syllabus_id)).filter(
                    CriterionOutcome.status == ResultStatus.not_compliant
                ),
            )
            .join(CriterionOutcome, CriterionOutcome.run_id == latest.c.run_id)
            .join(Criterion, Criterion.id == CriterionOutcome.criterion_id)
            .join(Principle, Principle.id == Criterion.principle_id)
            .where(CriterionOutcome.status.in_(ISSUE_STATUSES))
            .group_by(Criterion.code, Criterion.name, Criterion.critical, Principle.number, Principle.name)
            .order_by(sa.desc("n"), Criterion.code)
            .limit(limit)
        )
    ).all()
    return [
        {
            "criterion_id": code,
            "name": name,
            "critical": critical,
            "principle": number,
            "principle_name": principle_name,
            "syllabi": n,
            "not_compliant": not_compliant,
            "percent": round(100 * n / total) if total else 0,
        }
        for code, name, critical, number, principle_name, n, not_compliant in rows
    ]


async def runs_by_trigger(session: AsyncSession, principal: Principal, *, semester: str) -> list[dict]:
    rows = (
        await session.execute(
            sa.select(AssessmentRun.trigger, sa.func.count())
            .join(SyllabusVersion, SyllabusVersion.id == AssessmentRun.version_id)
            .join(Syllabus, Syllabus.id == SyllabusVersion.syllabus_id)
            .join(Course, Course.id == Syllabus.course_id)
            .join(Program, Program.id == Course.program_id)
            .where(Syllabus.semester == semester, syllabus_filter(principal))
            .group_by(AssessmentRun.trigger)
        )
    ).all()
    return [{"trigger": t, "runs": n} for t, n in rows]


async def overview(session: AsyncSession, principal: Principal, *, semester: str) -> list[dict]:
    """School cards on the Overview page: one row per School the principal answers for, empty or not.

    The joins down to Course are outer joins. A School's courses are created from the files the Drive
    sync finds, so a School whose folder is still empty has none; inner joins dropped it from the page,
    and the Provost Office saw one School of five. The scope filter sits in the Syllabus join, so it
    limits what is counted rather than which Schools are listed. There is no expected count: the
    University does not want one, and the School's own status per folder is read on Collection instead.
    """
    s = Syllabus
    listed: sa.ColumnElement[bool] = sa.true()
    if not principal.university_wide:
        # A School administrator's School, the Schools of a Head's programs, and any School where
        # something is visible to them (a professor's courses).
        own = sa.select(Program.school_id).where(Program.id.in_(principal.program_ids))
        listed = sa.or_(School.id.in_(principal.school_ids), School.id.in_(own), sa.func.count(s.id) > 0)
    rows = (
        await session.execute(
            sa.select(
                School.id,
                School.name,
                sa.func.count(s.id).label("found"),
                sa.func.count(s.id).filter(s.ai_status.in_([AiStatus.checked, AiStatus.needs_revision])),
                sa.func.count(s.id).filter(s.ai_status == AiStatus.needs_revision),
                sa.func.count(s.id).filter(
                    s.approval_stage.in_([ApprovalStage.head_review, ApprovalStage.kok_review])
                ),
                sa.func.count(s.id).filter(s.approval_stage == ApprovalStage.approved),
                sa.func.max(s.updated_at),
            )
            .select_from(School)
            .outerjoin(Program, Program.school_id == School.id)
            .outerjoin(Course, Course.program_id == Program.id)
            .outerjoin(
                s, sa.and_(s.course_id == Course.id, s.semester == semester, syllabus_filter(principal))
            )
            .group_by(School.id, School.name)
            .having(listed)
            .order_by(School.name)
        )
    ).all()
    return [
        {
            "school_id": sid,
            "school_name": name,
            "found": found,
            "checked": checked,
            "needs_revision": needs,
            "awaiting_approval": awaiting,
            "approved": approved,
            "last_activity": last,
        }
        for sid, name, found, checked, needs, awaiting, approved, last in rows
    ]


async def overview_csv(session: AsyncSession, principal: Principal, *, semester: str) -> str:
    rows = await overview(session, principal, semester=semester)
    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(["School", "Found", "Checked", "Needs revision", "Awaiting approval", "Approved"])
    for r in rows:
        writer.writerow(
            [
                r["school_name"],
                r["found"],
                r["checked"],
                r["needs_revision"],
                r["awaiting_approval"],
                r["approved"],
            ]
        )
    return buffer.getvalue()
