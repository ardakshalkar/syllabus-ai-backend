import uuid
from datetime import datetime

import sqlalchemy as sa
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.enums import FolderStatus, Role
from app.models.base import Base, CreatedAt, UUIDPk, str_enum

course_instructors = sa.Table(
    "course_instructors",
    Base.metadata,
    sa.Column("course_id", sa.ForeignKey("courses.id", ondelete="CASCADE"), primary_key=True),
    sa.Column("user_id", sa.ForeignKey("users.id", ondelete="CASCADE"), primary_key=True),
)


class User(UUIDPk, CreatedAt, Base):
    __tablename__ = "users"

    email: Mapped[str] = mapped_column(sa.String(254), unique=True)
    name: Mapped[str] = mapped_column(sa.String(200))
    title: Mapped[str | None] = mapped_column(sa.String(120))
    external_id: Mapped[str | None] = mapped_column(sa.String(255), unique=True)
    # scrypt hash (app/auth/passwords.py); None: this user cannot sign in with a password.
    password_hash: Mapped[str | None] = mapped_column(sa.String(255))
    is_active: Mapped[bool] = mapped_column(default=True)

    roles: Mapped[list["RoleAssignment"]] = relationship(back_populates="user", lazy="selectin")


class RoleAssignment(UUIDPk, Base):
    """A role, optionally scoped: school_admin → school_id, program_head → program_id."""

    __tablename__ = "role_assignments"
    __table_args__ = (sa.UniqueConstraint("user_id", "role", "school_id", "program_id"),)

    user_id: Mapped[uuid.UUID] = mapped_column(sa.ForeignKey("users.id", ondelete="CASCADE"), index=True)
    role: Mapped[Role] = mapped_column(str_enum(Role))
    school_id: Mapped[uuid.UUID | None] = mapped_column(sa.ForeignKey("schools.id", ondelete="CASCADE"))
    program_id: Mapped[uuid.UUID | None] = mapped_column(sa.ForeignKey("programs.id", ondelete="CASCADE"))

    user: Mapped[User] = relationship(back_populates="roles")


class School(UUIDPk, Base):
    __tablename__ = "schools"

    slug: Mapped[str] = mapped_column(sa.String(60), unique=True)
    name: Mapped[str] = mapped_column(sa.String(200))


class Program(UUIDPk, Base):
    __tablename__ = "programs"

    slug: Mapped[str] = mapped_column(sa.String(60), unique=True)
    name: Mapped[str] = mapped_column(sa.String(200))
    school_id: Mapped[uuid.UUID] = mapped_column(sa.ForeignKey("schools.id"), index=True)
    degree_level: Mapped[str] = mapped_column(sa.String(40))
    # Educational Program Passport text; criterion 3.4 can only be verified when it is present.
    passport_text: Mapped[str | None] = mapped_column(sa.Text)


class Course(UUIDPk, Base):
    __tablename__ = "courses"

    slug: Mapped[str] = mapped_column(sa.String(60), unique=True)
    name: Mapped[str] = mapped_column(sa.String(200))
    program_id: Mapped[uuid.UUID] = mapped_column(sa.ForeignKey("programs.id"), index=True)
    credits: Mapped[int]
    # None for a course read from Google Drive: the School's administrators act for it.
    lead_id: Mapped[uuid.UUID | None] = mapped_column(sa.ForeignKey("users.id"), index=True)
    # The Drive folder the syllabus file sits in (a course read from Drive); used to group and filter.
    discipline: Mapped[str | None] = mapped_column(sa.String(200))

    instructors: Mapped[list[User]] = relationship(secondary=course_instructors, lazy="selectin")


class UploadFolder(UUIDPk, CreatedAt, Base):
    """Folder a School uploads into. `drive_folder_id` is where its files land in Google Drive.

    A School keeps one of these per semester: it makes its own Drive folder, shares it with colleagues and
    with the app's service account, and registers the link here. What the School then says about the folder
    (`status`, `expected_count`) is its own claim, and is deliberately not derived from the files found in it.
    """

    __tablename__ = "upload_folders"
    __table_args__ = (sa.UniqueConstraint("school_id", "path"),)

    school_id: Mapped[uuid.UUID] = mapped_column(sa.ForeignKey("schools.id"), index=True)
    path: Mapped[str] = mapped_column(sa.String(300))
    semester: Mapped[str] = mapped_column(sa.String(40))
    drive_folder_id: Mapped[str | None] = mapped_column(sa.String(128))
    status: Mapped[FolderStatus] = mapped_column(
        str_enum(FolderStatus), default=FolderStatus.not_started
    )
    # How many syllabi the School says belong in this folder this semester. Without it nothing can be
    # missing: every course is otherwise created from a file that was found, so found always equals expected.
    expected_count: Mapped[int | None] = mapped_column(sa.Integer)
    declared_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True))
    declared_by_id: Mapped[uuid.UUID | None] = mapped_column(sa.ForeignKey("users.id"))
    declared_by: Mapped[User | None] = relationship(lazy="selectin")
