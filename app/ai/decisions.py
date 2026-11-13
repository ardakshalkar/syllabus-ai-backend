"""Decision models (OpenRouter's Decisions API): every criterion of a syllabus gets its status in one request.

A decision model reads a state (the syllabus) and answers typed questions with probabilities instead of writing
text, in about a second for all criteria. It gives no evidence, explanation or recommendation, so the chat model
still writes those, but only for the criteria that are not Compliant (see pipeline.evaluate).
"""

import asyncio
import json
import logging
import re
import time
import uuid
from dataclasses import dataclass
from typing import Any, Protocol

import httpx

from app.ai.llm import LLMError, _save_trace
from app.ai.types import CriterionSpec
from app.config import get_settings
from app.enums import ResultStatus

log = logging.getLogger(__name__)

STATUSES = [s.value for s in ResultStatus]


@dataclass
class Decision:
    status: ResultStatus
    confidence: float | None


class DecisionMaker(Protocol):
    """What pipeline.evaluate needs from a decision model; tests pass a fake."""

    model_version: str

    async def decide(
        self, state: list[str], specs: list[CriterionSpec], *, run_id: uuid.UUID | None = None, trace: bool = True
    ) -> dict[str, "Decision"]: ...


def question_id(code: str) -> str:
    """Question ids are object keys; criterion codes such as "3.5" become "c_3_5"."""
    return "c_" + re.sub(r"[^A-Za-z0-9_]", "_", code)


def build_questions(specs: list[CriterionSpec]) -> dict[str, Any]:
    questions: dict[str, Any] = {}
    for spec in specs:
        text = f"Syllabus quality criterion {spec.code} ({spec.principle_name}): {spec.name}"
        if spec.guidance:
            text += f"\nGuidance: {spec.guidance}"
        text += (
            "\nJudge whether the syllabus in the state meets this criterion. Compliant: clearly satisfied. "
            "Partially compliant: addressed, but incompletely or ambiguously. Not compliant: missing or "
            "contradicted. Cannot verify: only when the syllabus text alone cannot decide it."
        )
        questions[question_id(spec.code)] = {
            "type": "choice",
            "instructions": text,
            "criteria": {s: None for s in STATUSES},
        }
    return questions


def parse_answers(specs: list[CriterionSpec], answers: dict[str, Any]) -> dict[str, Decision]:
    """Criteria the model left out, or answered with something unknown, are missing from the result."""
    out: dict[str, Decision] = {}
    for spec in specs:
        answer = answers.get(question_id(spec.code)) or {}
        choice = answer.get("choice")
        if choice not in STATUSES:
            continue
        confidence = answer.get("confidence")
        out[spec.code] = Decision(ResultStatus(choice), float(confidence) if confidence is not None else None)
    return out


class Decider:
    def __init__(self) -> None:
        s = get_settings()
        self.model = s.decision_model
        self.model_version = s.decision_model
        self.url = s.decision_url
        self.max_retries = s.llm_max_retries
        key = s.openrouter_api_key or s.llm_api_key
        self.client = httpx.AsyncClient(
            timeout=s.llm_timeout_s, headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"}
        )

    async def decide(
        self,
        state: list[str],
        specs: list[CriterionSpec],
        *,
        run_id: uuid.UUID | None = None,
        trace: bool = True,
    ) -> dict[str, Decision]:
        body = {"model": self.model, "state": state, "questions": build_questions(specs)}
        last_error = ""
        for attempt in range(1, self.max_retries + 1):
            started = time.monotonic()
            data: dict[str, Any] | None = None
            error: str | None = None
            try:
                response = await self.client.post(self.url, json=body)
                if response.status_code >= 400:
                    error = f"HTTP {response.status_code}: {response.text[:300]}"
                else:
                    data = response.json()
            except (httpx.HTTPError, ValueError) as exc:
                error = f"{type(exc).__name__}: {exc}"
            duration = time.monotonic() - started
            usage = (data or {}).get("usage") or {}
            if trace:
                await _save_trace(
                    purpose="decisions",
                    run_id=run_id,
                    model=self.model,
                    messages=[{"role": "user", "content": json.dumps(body["questions"], ensure_ascii=False)}],
                    response=json.dumps(data, ensure_ascii=False) if data else None,
                    prompt_tokens=usage.get("input_tokens"),
                    completion_tokens=usage.get("output_tokens"),
                    duration_ms=int(duration * 1000),
                    queued_ms=0,
                    error=error,
                )
            if data is not None:
                decisions = parse_answers(specs, data.get("answers") or {})
                log.info(
                    "decisions: %s/%s criteria in %.1fs, %s tokens, $%s",
                    len(decisions), len(specs), duration, usage.get("input_tokens"), usage.get("cost"),
                )
                return decisions
            last_error = error or "no answer"
            log.warning("decisions attempt %s/%s failed: %s", attempt, self.max_retries, last_error)
            if attempt < self.max_retries:
                await asyncio.sleep(min(2**attempt, 20))
        raise LLMError(f"decision model failed after {self.max_retries} attempts: {last_error}")


def get_decider() -> Decider | None:
    """None when no decision model is configured: the chat model then decides every status itself."""
    return Decider() if get_settings().decision_model else None
