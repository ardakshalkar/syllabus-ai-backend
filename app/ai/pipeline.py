"""Full evaluation of one extracted syllabus. Pure: persistence is the caller's job (see services.checks)."""

import asyncio
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from itertools import groupby

from app.ai.checks.criteria import evaluate_principle
from app.ai.checks.technical import build_report, evaluate_rule
from app.ai.evidence import verify
from app.ai.llm import LLMProvider
from app.ai.types import CriterionResultData, CriterionSpec, EvaluationOutput, ExtractedContent
from app.config import get_settings
from app.enums import CheckKind, EvaluationType, ResultStatus

ALL_CHECKS = frozenset(CheckKind)

# Which session checkbox enables evaluating a criterion that needs this external context.
CONTEXT_CHECK = {
    "Educational Program Passport": CheckKind.program_alignment,
    "Narxoz library catalogue": CheckKind.library,
}

# What a check goes through, in order; Progress.step indexes this. The UI lists them as a checklist.
STEPS = (
    "Reading document",
    "Checking technical requirements",
    "Evaluating criteria",
    "Generating evidence",
    "Preparing recommendations",
)
READING, TECHNICAL, CRITERIA, EVIDENCE, RECOMMENDATIONS = range(len(STEPS))


@dataclass
class Progress:
    step: int
    criteria_total: int = 0
    principles_done: int = 0
    principles_total: int = 0
    # Principles whose LLM request is running now, e.g. "4. Assessment".
    running: list[str] = field(default_factory=list)

    @property
    def phase(self) -> str:
        if self.step == CRITERIA:
            return (
                f"Evaluating {self.criteria_total} criteria "
                f"({self.principles_done}/{self.principles_total} principles)"
            )
        return STEPS[self.step]


Phase = Callable[[Progress], Awaitable[None]]


async def _noop(_: Progress) -> None:
    return None


def _cannot_verify(spec: CriterionSpec, why: str) -> CriterionResultData:
    return CriterionResultData(
        criterion_id=spec.code,
        status=ResultStatus.cannot_verify,
        evidence="",
        explanation=f"Cannot verify this criterion because {why}.",
        recommendation=f"Connect the {spec.required_context} and run this criterion again."
        if spec.required_context
        else "Run this criterion again.",
        required_context=spec.required_context,
        confidence=None,
    )


async def evaluate(
    content: ExtractedContent,
    specs: list[CriterionSpec],
    llm: LLMProvider,
    *,
    checks: frozenset[CheckKind] | set[CheckKind] = ALL_CHECKS,
    context: dict[str, str] | None = None,
    run_id: uuid.UUID | None = None,
    trace: bool = True,
    on_phase: Phase = _noop,
) -> EvaluationOutput:
    settings = get_settings()
    context = context or {}

    await on_phase(Progress(TECHNICAL))
    report = build_report(content)
    if CheckKind.technical not in checks:
        report.issues = []

    results: dict[str, CriterionResultData] = {}
    to_llm: list[CriterionSpec] = []
    for spec in specs:
        if spec.evaluation_type == EvaluationType.deterministic:
            results[spec.code] = evaluate_rule(spec, content, report)
        elif spec.evaluation_type == EvaluationType.external_context:
            assert spec.required_context
            enabled = CONTEXT_CHECK.get(spec.required_context) in checks
            if enabled and context.get(spec.required_context):
                to_llm.append(spec)
            else:
                results[spec.code] = _cannot_verify(spec, f"the {spec.required_context} is unavailable")
        elif CheckKind.criteria in checks:
            to_llm.append(spec)

    batches = [
        list(group)
        for _, group in groupby(sorted(to_llm, key=lambda s: s.principle), key=lambda s: s.principle)
    ]
    progress = Progress(CRITERIA, criteria_total=len(specs), principles_total=len(batches))
    # Same limit as the LLM client's, applied here too so `running` lists only principles really in flight
    # (and, with one slot, they go in rubric order).
    slots = asyncio.Semaphore(settings.llm_max_concurrency)

    async def evaluate_batch(batch: list[CriterionSpec]) -> list[CriterionResultData]:
        name = f"{batch[0].principle}. {batch[0].principle_name}"
        async with slots:
            progress.running.append(name)
            await on_phase(progress)
            try:
                result = await evaluate_principle(
                    llm, batch, content, report, context, run_id=run_id, trace=trace
                )
            finally:
                progress.running.remove(name)
        progress.principles_done += 1
        await on_phase(progress)
        return result

    await on_phase(progress)
    tasks = [asyncio.ensure_future(evaluate_batch(b)) for b in batches]
    try:
        batch_results = await asyncio.gather(*tasks)
    except BaseException:
        # One principle failed or the run was cancelled: stop the LLM calls still waiting or running.
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        raise
    for batch in batch_results:
        for r in batch:
            results[r.criterion_id] = r

    await on_phase(Progress(EVIDENCE, len(specs), len(batches), len(batches)))
    ordered = [results[s.code] for s in specs if s.code in results]
    if CheckKind.evidence in checks:
        ordered = [verify(r, content.raw_text, settings.evidence_match_threshold) for r in ordered]
    else:
        ordered = [r.model_copy(update={"evidence": ""}) for r in ordered]

    await on_phase(Progress(RECOMMENDATIONS, len(specs), len(batches), len(batches)))
    if CheckKind.recommendations not in checks:
        ordered = [r.model_copy(update={"recommendation": ""}) for r in ordered]

    return EvaluationOutput(
        technical=report,
        results=ordered,
        model_version=llm.model_version,
        checker_version=settings.checker_version,
    )


def count_issues(results: list[CriterionResultData]) -> int:
    return sum(1 for r in results if r.status in (ResultStatus.partial, ResultStatus.not_compliant))
