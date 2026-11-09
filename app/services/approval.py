"""Academic approval workflow: Draft → AI Checked → Head of Program Review → KOK Review → Approved.

Draft → AI Checked happens automatically when the current version's check completes (services.checks).
"""

import uuid
from dataclasses import dataclass

import sqlalchemy as sa
from fastapi import HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.rbac import Principal, course_context, forbidden, manages_school
from app.enums import AiStatus, ApprovalStage, Role
from app.models import ApprovalEvent, Syllabus, User
from app.services.notifications import notify, users_with_role

S = ApprovalStage


@dataclass(frozen=True)
class Transition:
    actor: str  # "lead" | "program_head" | "kok"
    comment_required: bool = False
    label: str = ""


TRANSITIONS: dict[tuple[ApprovalStage, ApprovalStage], Transition] = {
    (S.ai_checked, S.head_review): Transition("lead", label="Submit to Head of Program"),
    (S.professor_revised, S.head_review): Transition("lead", label="Resubmit to Head of Program"),
    (S.head_review, S.kok_review): Transition("program_head", label="Approve to KOK"),
    (S.head_review, S.professor_revised): Transition("program_head", True, "Return for revision"),
    (S.kok_review, S.approved): Transition("kok", label="KOK approve syllabus"),
    (S.kok_review, S.ai_checked): Transition("kok", True, "Return to professor"),
}


async def _actor_kind(session: AsyncSession, principal: Principal, syllabus: Syllabus) -> set[str]:
    course, program = await course_context(session, syllabus.course_id)
    kinds = set()
    # A course read from Google Drive has no lead: whoever manages its School submits it instead.
    if course.lead_id == principal.id or (
        course.lead_id is None and manages_school(principal, program.school_id)
    ):
        kinds.add("lead")
    if principal.has(Role.program_head) and program.id in principal.program_ids:
        kinds.add("program_head")
    if principal.has(Role.kok):
        kinds.add("kok")
    return kinds


async def available_transitions(
    session: AsyncSession, principal: Principal, syllabus: Syllabus
) -> list[dict]:
    kinds = await _actor_kind(session, principal, syllabus)
    return [
        {"to_stage": to, "label": t.label, "comment_required": t.comment_required}
        for (frm, to), t in TRANSITIONS.items()
        if frm == syllabus.approval_stage and t.actor in kinds
    ]


async def transition(
    session: AsyncSession,
    principal: Principal,
    syllabus: Syllabus,
    to_stage: ApprovalStage,
    comment: str | None,
) -> ApprovalEvent:
    rule = TRANSITIONS.get((syllabus.approval_stage, to_stage))
    if rule is None:
        raise HTTPException(
            status.HTTP_409_CONFLICT, f"Cannot move from {syllabus.approval_stage.value} to {to_stage.value}"
        )
    if rule.actor not in await _actor_kind(session, principal, syllabus):
        raise forbidden(f"'{rule.label}' is reserved for the {rule.actor.replace('_', ' ')}")
    comment = (comment or "").strip() or None
    if rule.comment_required and not comment:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT, "A comment is required when returning a syllabus"
        )
    if rule.actor == "lead" and syllabus.ai_status not in (AiStatus.checked, AiStatus.needs_revision):
        raise HTTPException(
            status.HTTP_409_CONFLICT, "Wait for the AI check of the current version to complete"
        )

    from_stage = syllabus.approval_stage
    syllabus.approval_stage = to_stage
    event = ApprovalEvent(
        syllabus_id=syllabus.id,
        kind="transition",
        from_stage=from_stage,
        to_stage=to_stage,
        version=syllabus.current_version,
        actor_id=principal.id,
        comment=comment,
    )
    session.add(event)
    await _notify_transition(session, syllabus, to_stage, comment)
    await session.flush()
    return event


async def _notify_transition(
    session: AsyncSession, syllabus: Syllabus, to_stage: ApprovalStage, comment: str | None
) -> None:
    course, program = await course_context(session, syllabus.course_id)
    payload = {"syllabus_id": str(syllabus.id), "stage": to_stage.value}
    if to_stage == S.head_review:
        recipients = await users_with_role(session, Role.program_head, program_id=program.id)
        title = f"{course.name} syllabus is waiting for Head of Program review"
    elif to_stage == S.kok_review:
        recipients = await users_with_role(session, Role.kok)
        title = f"{course.name} syllabus is waiting for KOK review"
    elif to_stage == S.approved:
        recipients = [
            *([course.lead_id] if course.lead_id else []),
            *await users_with_role(session, Role.school_admin, school_id=program.school_id),
        ]
        title = f"{course.name} syllabus was approved"
    else:
        recipients = await _lead_or_school(session, course.lead_id, program.school_id)
        title = f"{course.name} syllabus was returned for revision"
    notify(session, recipients, kind="approval", title=title, body=comment, payload=payload)


async def _lead_or_school(
    session: AsyncSession, lead_id: uuid.UUID | None, school_id: uuid.UUID
) -> list[uuid.UUID]:
    """Who acts for the course: its lead, or for a course without one (from Drive) the School's admins."""
    if lead_id is not None:
        return [lead_id]
    return list(await users_with_role(session, Role.school_admin, school_id=school_id))


async def add_comment(
    session: AsyncSession, principal: Principal, syllabus: Syllabus, text: str
) -> ApprovalEvent:
    text = text.strip()
    if not text:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "Comment is empty")
    event = ApprovalEvent(
        syllabus_id=syllabus.id,
        kind="comment",
        version=syllabus.current_version,
        actor_id=principal.id,
        comment=text,
    )
    session.add(event)
    course, program = await course_context(session, syllabus.course_id)
    if course.lead_id != principal.id:
        notify(
            session,
            [
                r
                for r in await _lead_or_school(session, course.lead_id, program.school_id)
                if r != principal.id
            ],
            kind="comment",
            title=f"New reviewer comment on {course.name}",
            body=text,
            payload={"syllabus_id": str(syllabus.id)},
        )
    await session.flush()
    return event


async def activity(session: AsyncSession, syllabus_id: uuid.UUID) -> list[dict]:
    rows = (
        await session.execute(
            sa.select(ApprovalEvent, User.name)
            .outerjoin(User, User.id == ApprovalEvent.actor_id)
            .where(ApprovalEvent.syllabus_id == syllabus_id)
            .order_by(ApprovalEvent.created_at.desc(), ApprovalEvent.id)
        )
    ).all()
    return [
        {
            "id": e.id,
            "kind": e.kind,
            "from_stage": e.from_stage,
            "to_stage": e.to_stage,
            "version": e.version,
            "actor_id": e.actor_id,
            "actor_name": name or "Syllabus AI",
            "comment": e.comment,
            "created_at": e.created_at,
        }
        for e, name in rows
    ]
