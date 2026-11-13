from datetime import date

import sqlalchemy as sa
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, CreatedAt, UUIDPk


class Semester(UUIDPk, CreatedAt, Base):
    """A semester the University collects and checks syllabi for, kept by the Provost Office.

    Syllabi, Drive folders and checking sessions keep the semester as its name ("Fall 2026"), so this
    table is the list people pick from rather than a foreign key; that is why a semester's name cannot
    be changed once it is in use. One semester is current: the one the app opens on.
    """

    __tablename__ = "semesters"

    name: Mapped[str] = mapped_column(sa.String(40), unique=True)
    starts_on: Mapped[date | None] = mapped_column(sa.Date)
    ends_on: Mapped[date | None] = mapped_column(sa.Date)
    is_current: Mapped[bool] = mapped_column(default=False, server_default=sa.false())
