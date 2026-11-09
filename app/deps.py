from typing import Annotated

import sqlalchemy as sa
from fastapi import Depends, HTTPException, Request, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.client import AuthServiceUnavailable, Identity, InvalidToken, get_auth_client
from app.auth.rbac import Principal, forbidden
from app.db.pg import get_session
from app.enums import Role
from app.models import User

DbSession = Annotated[AsyncSession, Depends(get_session)]


def _token(request: Request) -> str | None:
    header = request.headers.get("authorization", "")
    if header.lower().startswith("bearer "):
        return header[7:].strip()
    return None


async def _provisioned_user(session: AsyncSession, identity: Identity) -> User | None:
    """Users must already exist here with their roles. An email links only an account that is not linked yet."""
    user = await session.scalar(sa.select(User).where(User.external_id == identity.external_id))
    if user is not None:
        return user
    user = await session.scalar(
        sa.select(User).where(sa.func.lower(User.email) == identity.email, User.external_id.is_(None))
    )
    if user is not None:
        user.external_id = identity.external_id
        await session.commit()
    return user


async def get_principal(request: Request, session: DbSession) -> Principal:
    token = _token(request)
    if not token:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Not authenticated")
    try:
        identity = await get_auth_client().verify_token(token)
    except InvalidToken as exc:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid or expired token") from exc
    except AuthServiceUnavailable as exc:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "Auth service is unavailable") from exc
    user = await _provisioned_user(session, identity)
    if user is None or not user.is_active:
        raise forbidden("This account has no access to Syllabus AI")
    return Principal.of(user)


CurrentUser = Annotated[Principal, Depends(get_principal)]


def require_role(*roles: Role):
    async def dependency(principal: CurrentUser) -> Principal:
        if not principal.has(*roles):
            raise forbidden(f"Requires role: {', '.join(r.value for r in roles)}")
        return principal

    return Depends(dependency)
