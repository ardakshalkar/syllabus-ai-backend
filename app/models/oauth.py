import uuid
from datetime import datetime

import sqlalchemy as sa
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, CreatedAt, UUIDPk


class OAuthClient(UUIDPk, CreatedAt, Base):
    """An app that registered itself (RFC 7591) to ask for access, e.g. Claude. A public client: no secret, PKCE."""

    __tablename__ = "oauth_clients"

    client_id: Mapped[str] = mapped_column(sa.String(60), unique=True)
    # Self-declared, so the consent screen also shows where the app will be sent back to.
    client_name: Mapped[str] = mapped_column(sa.String(200))
    redirect_uris: Mapped[list[str]] = mapped_column(sa.JSON)


class OAuthGrant(UUIDPk, CreatedAt, Base):
    """One connection: a user let one app act for them with one scope, until they (or an administrator) revoke it.

    The tokens are random strings; only their SHA-256 is kept. Refreshing replaces both tokens and remembers the
    refresh token just spent, so a copy of it used later (a stolen one) ends the whole connection.
    """

    __tablename__ = "oauth_grants"

    user_id: Mapped[uuid.UUID] = mapped_column(sa.ForeignKey("users.id", ondelete="CASCADE"), index=True)
    client_id: Mapped[uuid.UUID] = mapped_column(sa.ForeignKey("oauth_clients.id", ondelete="CASCADE"))
    scope: Mapped[str] = mapped_column(sa.String(40))
    access_hash: Mapped[str] = mapped_column(sa.String(64), unique=True)
    access_expires_at: Mapped[datetime] = mapped_column(sa.DateTime(timezone=True))
    refresh_hash: Mapped[str] = mapped_column(sa.String(64), unique=True)
    refresh_expires_at: Mapped[datetime] = mapped_column(sa.DateTime(timezone=True))
    previous_refresh_hash: Mapped[str | None] = mapped_column(sa.String(64), index=True)
    last_used_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True))
    revoked_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True))
