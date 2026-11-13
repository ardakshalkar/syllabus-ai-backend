import sqlalchemy as sa
from fastapi import APIRouter

from app.deps import CurrentUser, DbSession
from app.models import Course, course_instructors
from app.schemas.catalog import MeOut, RoleOut

router = APIRouter(tags=["me"])


@router.get("/me", response_model=MeOut)
async def me(principal: CurrentUser, session: DbSession) -> MeOut:
    led = list(await session.scalars(sa.select(Course.id).where(Course.lead_id == principal.id)))
    taught = list(
        await session.scalars(
            sa.select(course_instructors.c.course_id).where(course_instructors.c.user_id == principal.id)
        )
    )
    u = principal.user
    return MeOut(
        id=u.id,
        email=u.email,
        name=u.name,
        title=u.title,
        roles=[RoleOut.model_validate(r) for r in u.roles],
        led_course_ids=led,
        taught_course_ids=[c for c in taught if c not in led],
    )
