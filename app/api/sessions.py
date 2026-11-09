import uuid

import sqlalchemy as sa
from fastapi import APIRouter, Query, Response, status

from app.deps import CurrentUser, DbSession
from app.models import CheckSession, User
from app.schemas.work import SessionCreate, SessionItemOut, SessionOut
from app.services import sessions
from app.workers import dispatch

router = APIRouter(tags=["sessions"])


async def _out(session: DbSession, cs: CheckSession) -> SessionOut:
    await session.refresh(cs)
    # The session list shows who started a check, so the name travels with the row rather than
    # leaving the browser to resolve each id. Repeat lookups are served by the identity map.
    creator = await session.get(User, cs.created_by)
    return SessionOut.model_validate(
        {
            **{c.key: getattr(cs, c.key) for c in CheckSession.__table__.columns},
            "created_by_name": creator.name if creator else "",
            "progress": await sessions.progress(session, cs.id),
        }
    )


@router.get("/sessions", response_model=list[SessionOut])
async def list_sessions(principal: CurrentUser, session: DbSession):
    return [await _out(session, cs) for cs in await sessions.list_sessions(session, principal)]


@router.post("/sessions", response_model=SessionOut, status_code=status.HTTP_201_CREATED)
async def create(body: SessionCreate, principal: CurrentUser, session: DbSession):
    cs, run_ids = await sessions.create(session, principal, **body.model_dump())
    await session.commit()
    await dispatch.check_runs(run_ids)
    return await _out(session, cs)


@router.get("/sessions/{session_id}", response_model=SessionOut)
async def get(session_id: uuid.UUID, principal: CurrentUser, session: DbSession):
    return await _out(session, await sessions.get_session_obj(session, principal, session_id))


@router.get("/sessions/{session_id}/items", response_model=list[SessionItemOut])
async def items(
    session_id: uuid.UUID,
    principal: CurrentUser,
    session: DbSession,
    limit: int = Query(50, ge=1, le=500),
    offset: int = Query(0, ge=0),
):
    await sessions.get_session_obj(session, principal, session_id)
    return await sessions.items(session, session_id, limit=limit, offset=offset)


@router.delete("/sessions/{session_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete(session_id: uuid.UUID, principal: CurrentUser, session: DbSession) -> Response:
    cs = await sessions.get_session_obj(session, principal, session_id)
    await sessions.delete(session, principal, cs)
    await session.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post("/sessions/{session_id}/pause", response_model=SessionOut)
async def pause(session_id: uuid.UUID, principal: CurrentUser, session: DbSession):
    cs = await sessions.get_session_obj(session, principal, session_id)
    await sessions.pause(session, principal, cs)
    await session.commit()
    return await _out(session, cs)


@router.post("/sessions/{session_id}/resume", response_model=SessionOut)
async def resume(session_id: uuid.UUID, principal: CurrentUser, session: DbSession):
    cs = await sessions.get_session_obj(session, principal, session_id)
    run_ids = await sessions.resume(session, principal, cs)
    await session.commit()
    await dispatch.check_runs(run_ids)
    return await _out(session, cs)
