"""LLM evaluation of rubric criteria, one call per principle."""

import json
import logging
import uuid
from typing import Any

from app.ai.llm import LLMProvider
from app.ai.types import CriterionResultData, CriterionSpec, ExtractedContent, TechnicalReport
from app.config import get_settings
from app.enums import ResultStatus

log = logging.getLogger(__name__)

MAX_DOCUMENT_CHARS = 60_000
NO_CHANGE = "No change suggested for this criterion."
# Context budget besides the syllabus: system prompt, criteria and the answer (about 700 tokens, up to 1.5k).
_RESERVED_TOKENS = 4_000
# Conservative for Cyrillic text, which takes more tokens per character than English.
_CHARS_PER_TOKEN = 2.5

# Shared with the decision model's questions (app.ai.decisions). A syllabus that lacks what a criterion asks for
# fails it: "Cannot verify" is kept for what no syllabus could show, so it never hides a missing section.
MISSING_RULE = (
    "When the content the criterion depends on is absent or empty in the syllabus (for example no list of topics, "
    "no schedule, no assessment criteria), the status is Not compliant."
)
CANNOT_VERIFY_RULE = (
    "only when deciding needs information that a syllabus is not expected to contain, such as an external "
    "document. Never use it because a section of the syllabus is missing or empty."
)

SYSTEM_PROMPT = """You review university course syllabi for Narxoz University against a quality rubric.
For every criterion you are given, decide one status:
- "Compliant": the syllabus clearly satisfies the criterion.
- "Partially compliant": it is addressed, but incompletely or ambiguously.
- "Not compliant": it is missing or contradicted. {missing_rule}
- "Cannot verify": {cannot_verify_rule}
Rules:
- "evidence" MUST be copied verbatim from the syllabus: one short passage, at most 200 characters. Never paraphrase.
  When something is missing, quote the closest related passage (for example the outcome that is not assessed).
- "explanation" says why the evidence leads to the status, in one short sentence.
- "recommendation" is one concrete edit the professor can make, in one sentence. Leave it empty ("") when the status
  is "Compliant".
- "confidence" is your confidence in the status between 0 and 1.
- Write explanation and recommendation in {language}. Keep evidence in the syllabus's original language.
- Judge only what is written. Do not assume content that is not in the document.
- Be brief: the answer is read by software, not by people."""


# When a decision model has already given each criterion its status (app.ai.decisions), the chat model only
# writes the text that goes with it.
EXPLAIN_SYSTEM_PROMPT = """You review university course syllabi for Narxoz University against a quality rubric.
Every criterion you are given already has its status, decided by a separate reviewer. Do not change it.
Statuses mean:
- "Partially compliant": addressed, but incompletely or ambiguously.
- "Not compliant": missing or contradicted. {missing_rule}
- "Cannot verify": {cannot_verify_rule}
For every criterion write:
- "evidence": copied verbatim from the syllabus: one short passage, at most 200 characters. Never paraphrase.
  When something is missing, quote the closest related passage (for example the outcome that is not assessed).
- "explanation": why the evidence leads to the given status, in one short sentence.
- "recommendation": one concrete edit the professor can make, in one sentence.
- Write explanation and recommendation in {language}. Keep evidence in the syllabus's original language.
- Judge only what is written. Do not assume content that is not in the document.
- Be brief: the answer is read by software, not by people."""


def explain_schema(codes: list[str]) -> dict[str, Any]:
    return {
        "type": "object",
        "additionalProperties": False,
        "required": ["results"],
        "properties": {
            "results": {
                "type": "array",
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["criterion_id", "evidence", "explanation", "recommendation"],
                    "properties": {
                        "criterion_id": {"type": "string", "enum": codes},
                        "evidence": {"type": "string"},
                        "explanation": {"type": "string"},
                        "recommendation": {"type": "string"},
                    },
                },
            }
        },
    }


def response_schema(codes: list[str]) -> dict[str, Any]:
    return {
        "type": "object",
        "additionalProperties": False,
        "required": ["results"],
        "properties": {
            "results": {
                "type": "array",
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": [
                        "criterion_id",
                        "status",
                        "evidence",
                        "explanation",
                        "recommendation",
                        "confidence",
                    ],
                    "properties": {
                        "criterion_id": {"type": "string", "enum": codes},
                        "status": {"type": "string", "enum": [s.value for s in ResultStatus]},
                        "evidence": {"type": "string"},
                        "explanation": {"type": "string"},
                        "recommendation": {"type": "string"},
                        "confidence": {"type": "number", "minimum": 0, "maximum": 1},
                    },
                },
            }
        },
    }


def max_document_chars() -> int:
    """With Ollama the context is LLM_NUM_CTX, and Ollama silently drops the start of a longer prompt (the
    instructions), so the syllabus is cut to what fits instead."""
    s = get_settings()
    if s.llm_provider != "ollama" or not s.llm_num_ctx:
        return MAX_DOCUMENT_CHARS
    fits = int((s.llm_num_ctx - _RESERVED_TOKENS) * _CHARS_PER_TOKEN)
    return max(4_000, min(MAX_DOCUMENT_CHARS, fits))


def document_text(content: ExtractedContent) -> str:
    text = content.raw_text
    limit = max_document_chars()
    if len(text) <= limit:
        return text
    log.warning(
        "syllabus has %s characters, only the first %s are evaluated (raise LLM_NUM_CTX to send more)",
        len(text),
        limit,
    )
    return text[:limit] + "\n[document truncated]"


def build_user_prompt(
    specs: list[CriterionSpec],
    content: ExtractedContent,
    report: TechnicalReport,
    context: dict[str, str],
    statuses: dict[str, ResultStatus] | None = None,
) -> str:
    """The syllabus and the structure findings come first and are the same for every principle, so the model
    server can reuse that part of the prompt (prefix cache) for the next principle instead of reading it again.
    `statuses`: the status already decided for each criterion, which the model explains instead of deciding."""
    criteria = [
        {
            "criterion_id": s.code,
            "criterion": s.name,
            **({"guidance": s.guidance} if s.guidance else {}),
            **({"status": statuses[s.code].value} if statuses and s.code in statuses else {}),
        }
        for s in specs
    ]
    parts = [f"Syllabus:\n<<<\n{document_text(content)}\n>>>"]
    if report.issues:
        parts.append(
            "Automated structure findings (reliable, use them):\n"
            + "\n".join(f"- {i.message}" for i in report.issues)
        )
    for name in sorted(
        {s.required_context for s in specs if s.required_context and s.required_context in context}
    ):
        parts.append(f"{name}:\n{context[name]}")
    parts.append(f"Principle {specs[0].principle}: {specs[0].principle_name}")
    parts.append("Criteria to evaluate:\n" + json.dumps(criteria, ensure_ascii=False))
    return "\n\n".join(parts)


async def evaluate_principle(
    llm: LLMProvider,
    specs: list[CriterionSpec],
    content: ExtractedContent,
    report: TechnicalReport,
    context: dict[str, str],
    *,
    run_id: uuid.UUID | None = None,
    trace: bool = True,
) -> list[CriterionResultData]:
    codes = [s.code for s in specs]
    data = await llm.complete_json(
        purpose="criteria",
        system=SYSTEM_PROMPT.format(
            language=get_settings().llm_output_language,
            missing_rule=MISSING_RULE,
            cannot_verify_rule=CANNOT_VERIFY_RULE,
        ),
        user=build_user_prompt(specs, content, report, context),
        schema=response_schema(codes),
        meta={"criteria": specs, "content": content, "report": report, "context": context},
        run_id=run_id,
        trace=trace,
    )
    by_code: dict[str, CriterionResultData] = {}
    for item in data.get("results", []):
        code = item.get("criterion_id")
        if code in codes and code not in by_code:
            status = ResultStatus(item["status"])
            recommendation = (item.get("recommendation") or "").strip()
            by_code[code] = CriterionResultData(
                criterion_id=code,
                status=status,
                evidence=(item.get("evidence") or "").strip(),
                explanation=(item.get("explanation") or "").strip(),
                # The model leaves it empty for Compliant criteria, which saves tokens; the text is ours.
                recommendation=recommendation or (NO_CHANGE if status == ResultStatus.compliant else ""),
                confidence=item.get("confidence"),
            )
    for spec in specs:
        if spec.code not in by_code:
            by_code[spec.code] = CriterionResultData(
                criterion_id=spec.code,
                status=ResultStatus.cannot_verify,
                evidence="",
                explanation="The evaluator returned no result for this criterion.",
                recommendation="Run the check again.",
                confidence=None,
            )
    return [by_code[c] for c in codes]


async def explain_principle(
    llm: LLMProvider,
    specs: list[CriterionSpec],
    statuses: dict[str, ResultStatus],
    confidences: dict[str, float | None],
    content: ExtractedContent,
    report: TechnicalReport,
    context: dict[str, str],
    *,
    run_id: uuid.UUID | None = None,
    trace: bool = True,
) -> list[CriterionResultData]:
    """Evidence, explanation and recommendation for criteria whose status a decision model has given."""
    codes = [s.code for s in specs]
    data = await llm.complete_json(
        purpose="explain",
        system=EXPLAIN_SYSTEM_PROMPT.format(
            language=get_settings().llm_output_language,
            missing_rule=MISSING_RULE,
            cannot_verify_rule=CANNOT_VERIFY_RULE,
        ),
        user=build_user_prompt(specs, content, report, context, statuses),
        schema=explain_schema(codes),
        meta={"criteria": specs, "statuses": statuses, "content": content, "report": report, "context": context},
        run_id=run_id,
        trace=trace,
    )
    written: dict[str, dict[str, Any]] = {}
    for item in data.get("results", []):
        code = item.get("criterion_id")
        if code in codes and code not in written:
            written[code] = item
    out = []
    for code in codes:
        item = written.get(code, {})
        out.append(
            CriterionResultData(
                criterion_id=code,
                status=statuses[code],
                evidence=(item.get("evidence") or "").strip(),
                explanation=(item.get("explanation") or "").strip()
                or "The evaluator returned no explanation for this criterion.",
                recommendation=(item.get("recommendation") or "").strip(),
                confidence=confidences.get(code),
            )
        )
    return out


def decided_compliant(code: str, confidence: float | None) -> CriterionResultData:
    """A criterion the decision model passed: no chat model request, so no quoted evidence."""
    sure = f" ({round(confidence * 100)}% confidence)" if confidence is not None else ""
    return CriterionResultData(
        criterion_id=code,
        status=ResultStatus.compliant,
        evidence="",
        explanation=f"Judged compliant by the decision model{sure}.",
        recommendation=NO_CHANGE,
        confidence=confidence,
    )
