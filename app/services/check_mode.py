"""How the AI checks a syllabus against the rubric, chosen by the administrator (Administration → AI check).

- "standard": as configured in .env. With DECISION_MODEL, a decision model gives every criterion its status in
  one request and the chat model (LLM_MODEL) writes evidence and explanations, one request per principle;
  without it, the chat model decides and explains, one request per principle.
- "single": one model decides and explains every criterion in ONE request, which reads the syllabus once
  instead of about ten times. The model is one of CHECK_MODELS. Measured on 20 syllabi (Nov 2026): Gemma 4 31B
  about $0.005 per syllabus, Qwen 4 Max about $0.035 and closest to the reference labels.

A check session keeps the mode it was created with (CheckSession.check_mode/check_model), so it runs the way its
estimate was priced even if the administrator changes the setting while it runs. Runs without a session (the
private pre-check) use the current setting.
"""

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from fastapi import HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.ai.decisions import DecisionMaker, get_decider
from app.ai.llm import LLMProvider, OllamaProvider, VllmProvider, get_llm
from app.config import get_settings
from app.models import AppSetting, CheckSession, User

KEY = "check_mode"
STANDARD = "standard"
SINGLE = "single"
MODES = (STANDARD, SINGLE)


@dataclass(frozen=True)
class CheckMode:
    mode: str = STANDARD
    model: str | None = None  # SINGLE only

    @property
    def label(self) -> str:
        if self.mode == SINGLE:
            return f"One request: {model_name(self.model or '')}"
        s = get_settings()
        if s.decision_model and s.llm_provider != "fake":
            return f"Two models: {model_name(s.decision_model)} + {model_name(s.llm_model)}"
        return f"One request per principle: {model_name(s.llm_model)}"


def model_name(model_id: str) -> str:
    """A readable name for a model id: "qwen/qwen4-max" → "qwen4-max"."""
    return model_id.rsplit("/", 1)[-1]


def allowed_models() -> list[str]:
    return [m.strip() for m in get_settings().check_models.split(",") if m.strip()]


def of_session(cs: CheckSession | None) -> CheckMode | None:
    """The mode a session was created with; None when it has none (created before the setting existed)."""
    if cs is None or not cs.check_mode:
        return None
    return CheckMode(cs.check_mode, cs.check_model)


async def current(session: AsyncSession) -> CheckMode:
    row = await session.get(AppSetting, KEY)
    if row is None:
        return CheckMode()
    mode, model = row.value.get("mode"), row.value.get("model")
    if mode == SINGLE and model:
        return CheckMode(SINGLE, model)
    return CheckMode()


async def describe(session: AsyncSession) -> dict[str, Any]:
    """The current setting, who changed it and when."""
    row = await session.get(AppSetting, KEY)
    updated_by_name = None
    updated_at: datetime | None = None
    if row is not None:
        updated_at = row.updated_at
        if row.updated_by:
            user = await session.get(User, row.updated_by)
            updated_by_name = user.name if user else None
    mode = await current(session)
    return {
        "mode": mode.mode,
        "model": mode.model,
        "label": mode.label,
        "updated_at": updated_at,
        "updated_by_name": updated_by_name,
    }


async def change(session: AsyncSession, mode: str, model: str | None, user_id: uuid.UUID) -> CheckMode:
    if mode not in MODES:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, f"Unknown check mode: {mode}")
    if mode == SINGLE and model not in allowed_models():
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT, "Choose one of the models offered for one-request checks"
        )
    value = {"mode": mode, "model": model if mode == SINGLE else None}
    row = await session.get(AppSetting, KEY)
    now = datetime.now(UTC)
    if row is None:
        session.add(AppSetting(key=KEY, value=value, updated_by=user_id, updated_at=now))
    else:
        row.value, row.updated_by, row.updated_at = value, user_id, now
    await session.commit()  # API sessions do not commit by themselves (db.pg.get_session)
    return CheckMode(mode, value["model"])


@dataclass
class Evaluator:
    """What pipeline.evaluate is given for a mode."""

    llm: LLMProvider
    decider: DecisionMaker | None
    criteria_calls: int  # 0: one request per principle; 1: every criterion in one request


def evaluator(mode: CheckMode) -> Evaluator:
    if mode.mode == SINGLE and mode.model:
        s = get_settings()
        llm: LLMProvider
        if s.llm_provider == "fake":  # tests and local runs without a model: it stands in for any model
            llm = get_llm()
        elif s.llm_provider == "ollama":
            llm = OllamaProvider(mode.model)
        else:
            llm = VllmProvider(mode.model)
        return Evaluator(llm, None, 1)
    return Evaluator(get_llm(), get_decider(), 0)
