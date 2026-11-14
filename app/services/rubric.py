import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, model_validator
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.ai.types import CriterionSpec
from app.enums import EvaluationType
from app.models import Criterion, Principle, RubricVersion

_TYPES = {
    "ai": EvaluationType.ai_semantic,
    "deterministic": EvaluationType.deterministic,
    "external": EvaluationType.external_context,
}


class _CriterionIn(BaseModel):
    name: str
    evaluation_type: Literal["ai", "deterministic", "external"] = "ai"
    rule: str | None = None
    required_context: str | None = None
    guidance: str | None = None
    critical: bool = False

    @model_validator(mode="after")
    def _check(self) -> "_CriterionIn":
        if self.evaluation_type == "deterministic" and not self.rule:
            raise ValueError(f"deterministic criterion '{self.name}' needs a rule")
        if self.evaluation_type == "external" and not self.required_context:
            raise ValueError(f"external criterion '{self.name}' needs required_context")
        return self


class _PrincipleIn(BaseModel):
    number: int
    name: str
    criteria: list[_CriterionIn]


class RubricFile(BaseModel):
    code: str
    title: str
    principles: list[_PrincipleIn]


class RubricExistsError(Exception):
    pass


async def import_rubric(session: AsyncSession, path: Path, *, activate: bool = True) -> RubricVersion:
    from app.ai.checks.technical import RULES

    data = RubricFile.model_validate(yaml.safe_load(path.read_text(encoding="utf-8")))  # noqa: ASYNC240 (CLI only)
    if await session.scalar(select(RubricVersion).where(RubricVersion.code == data.code)):
        raise RubricExistsError(f"rubric {data.code} already imported; publish changes under a new code")
    for p in data.principles:
        for c in p.criteria:
            if c.rule and c.rule not in RULES:
                raise ValueError(f"unknown rule '{c.rule}' (known: {', '.join(sorted(RULES))})")

    rubric = RubricVersion(code=data.code, title=data.title, is_active=False)
    session.add(rubric)
    await session.flush()
    for p in data.principles:
        principle = Principle(rubric_version_id=rubric.id, number=p.number, name=p.name)
        session.add(principle)
        await session.flush()
        for i, c in enumerate(p.criteria, start=1):
            session.add(
                Criterion(
                    rubric_version_id=rubric.id,
                    principle_id=principle.id,
                    code=f"{p.number}.{i}",
                    position=i,
                    name=c.name,
                    evaluation_type=_TYPES[c.evaluation_type],
                    rule=c.rule,
                    required_context=c.required_context,
                    guidance=c.guidance,
                    critical=c.critical,
                )
            )
    await session.flush()
    if activate:
        await session.execute(
            update(RubricVersion).where(RubricVersion.id != rubric.id).values(is_active=False)
        )
        rubric.is_active = True
    return rubric


async def set_critical(session: AsyncSession, path: Path) -> list[str]:
    """Copy the `critical` flags of a rubric YAML onto the version already imported under its code, and
    return the codes of the criteria now critical. Nothing else may differ: a changed criterion is a new
    version, but which findings count as critical only changes how results are reported."""
    data = RubricFile.model_validate(yaml.safe_load(path.read_text(encoding="utf-8")))  # noqa: ASYNC240 (CLI only)
    rubric = await session.scalar(select(RubricVersion).where(RubricVersion.code == data.code))
    if rubric is None:
        raise LookupError(f"rubric {data.code} is not imported; run `python -m app.cli import-rubric` first")
    stored = {p.number: p for p in rubric.principles}
    flagged: list[str] = []
    for p in data.principles:
        principle = stored.get(p.number)
        if principle is None or len(principle.criteria) != len(p.criteria):
            raise ValueError(f"principle {p.number} differs from imported rubric {data.code}")
        for c, criterion in zip(p.criteria, principle.criteria, strict=True):
            if c.name != criterion.name:
                raise ValueError(
                    f"criterion {criterion.code} is '{criterion.name}' in rubric {data.code}, not '{c.name}'"
                )
            criterion.critical = c.critical
            if c.critical:
                flagged.append(criterion.code)
    await session.flush()
    return flagged


@dataclass
class LoadedRubric:
    version: RubricVersion
    specs: list[CriterionSpec]
    criterion_ids: dict[str, uuid.UUID]
    principle_names: dict[int, str]


async def load_rubric(session: AsyncSession, rubric_version_id: uuid.UUID | None = None) -> LoadedRubric:
    query = select(RubricVersion)
    query = (
        query.where(RubricVersion.id == rubric_version_id)
        if rubric_version_id
        else query.where(RubricVersion.is_active.is_(True))
    )
    rubric = await session.scalar(query)
    if rubric is None:
        raise LookupError("no active rubric; run `python -m app.cli import-rubric`")
    specs, ids = [], {}
    for p in rubric.principles:
        for c in p.criteria:
            ids[c.code] = c.id
            specs.append(
                CriterionSpec(
                    code=c.code,
                    name=c.name,
                    principle=p.number,
                    principle_name=p.name,
                    evaluation_type=c.evaluation_type,
                    required_context=c.required_context,
                    rule=c.rule,
                    guidance=c.guidance,
                    critical=c.critical,
                )
            )
    return LoadedRubric(rubric, specs, ids, {p.number: p.name for p in rubric.principles})
