import uuid
from datetime import date
from typing import Annotated

from pydantic import Field, StringConstraints, model_validator

from app.schemas.common import Schema

Name = Annotated[str, StringConstraints(strip_whitespace=True, min_length=3, max_length=40)]


class SemesterOut(Schema):
    id: uuid.UUID
    name: str
    starts_on: date | None
    ends_on: date | None
    is_current: bool
    # Syllabi, Drive folders and checking sessions that name this semester; one in use cannot be deleted.
    in_use: int = 0


class SemesterIn(Schema):
    name: Name
    starts_on: date | None = None
    ends_on: date | None = None
    is_current: bool = False

    @model_validator(mode="after")
    def _dates_in_order(self) -> "SemesterIn":
        if self.starts_on and self.ends_on and self.ends_on < self.starts_on:
            raise ValueError("The semester must end after it starts")
        return self


class SemesterPatch(Schema):
    """The name is not here: syllabi, folders and sessions keep it as text, so a rename would orphan them."""

    starts_on: date | None = Field(default=None)
    ends_on: date | None = Field(default=None)
    is_current: bool | None = None
