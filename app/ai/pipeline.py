"""Full evaluation of one extracted syllabus. Pure: persistence is the caller's job (see services.checks)."""

import asyncio
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from itertools import groupby
from typing import cast

from app.ai.checks.criteria import decided_compliant, document_text, evaluate_principle, explain_principle
from app.ai.checks.technical import build_report, evaluate_rule
from app.ai.decisions import Decision, DecisionMaker, get_decider
from app.ai.evidence import verify
from app.ai.llm import LLMProvider
from app.ai.types import (
    CriterionResultData,
    CriterionSpec,
    EvaluationOutput,
    ExtractedContent,
    TechnicalReport,
)
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


# evaluate()'s default for `decider`: the decision model configured in settings, if any.
_FROM_SETTINGS = object()


def _state(content: ExtractedContent, report: TechnicalReport) -> list[str]:
    """What the decision model reads: the syllabus, and the structure findings the chat model is also given."""
    state = [f"Syllabus:\n{document_text(content)}"]
    if report.issues:
        state.append("Automated structure findings (reliable):\n" + "\n".join(f"- {i.message}" for i in report.issues))
    return state


def _by_principle(specs: list[CriterionSpec]) -> list[list[CriterionSpec]]:
    return [list(g) for _, g in groupby(sorted(specs, key=lambda s: s.principle), key=lambda s: s.principle)]


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
    decider: "DecisionMaker | None | object" = _FROM_SETTINGS,
) -> EvaluationOutput:
    """With a decision model (`decider`), it gives every criterion its status in one request and the chat model
    (`llm`) only writes evidence, explanation and recommendation for those that are not Compliant. Without one,
    the chat model does both, one request per principle."""
    settings = get_settings()
    context = context or {}
    dm = cast("DecisionMaker | None", get_decider() if decider is _FROM_SETTINGS else decider)

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

    decisions: dict[str, Decision] = {}
    if dm is not None and to_llm:
        progress = Progress(CRITERIA, criteria_total=len(specs), running=["All criteria (decision model)"])
        await on_phase(progress)
        decisions = await dm.decide(_state(content, report), to_llm, run_id=run_id, trace=trace)
        for code, d in decisions.items():
            if d.status == ResultStatus.compliant:
                results[code] = decided_compliant(code, d.confidence)
    # Explained: decided but not Compliant. Undecided (no decision model, or it skipped a criterion): the chat
    # model decides those itself.
    to_explain = [s for s in to_llm if s.code in decisions and s.code not in results]
    undecided = [s for s in to_llm if s.code not in decisions]
    statuses = {code: d.status for code, d in decisions.items()}
    confidences = {code: d.confidence for code, d in decisions.items()}
    jobs = [(b, True) for b in _by_principle(to_explain)] + [(b, False) for b in _by_principle(undecided)]
    progress = Progress(CRITERIA, criteria_total=len(specs), principles_total=len(jobs))
    # Same limit as the LLM client's, applied here too so `running` lists only principles really in flight
    # (and, with one slot, they go in rubric order).
    slots = asyncio.Semaphore(settings.llm_max_concurrency)

    async def evaluate_batch(batch: list[CriterionSpec], explain: bool) -> list[CriterionResultData]:
        name = f"{batch[0].principle}. {batch[0].principle_name}"
        async with slots:
            progress.running.append(name)
            await on_phase(progress)
            try:
                if explain:
                    result = await explain_principle(
                        llm, batch, statuses, confidences, content, report, context, run_id=run_id, trace=trace
                    )
                else:
                    result = await evaluate_principle(
                        llm, batch, content, report, context, run_id=run_id, trace=trace
                    )
            finally:
                progress.running.remove(name)
        progress.principles_done += 1
        await on_phase(progress)
        return result

    batches = jobs  # for the progress counts below
    model_version = f"{dm.model_version}+{llm.model_version}" if dm is not None and decisions else llm.model_version
    await on_phase(progress)
    tasks = [asyncio.ensure_future(evaluate_batch(b, explain)) for b, explain in jobs]
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
        model_version=model_version,
        checker_version=settings.checker_version,
    )


def count_issues(results: list[CriterionResultData]) -> int:
    return sum(1 for r in results if r.status in (ResultStatus.partial, ResultStatus.not_compliant))
