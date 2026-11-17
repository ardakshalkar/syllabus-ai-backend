"""LLM evaluation of rubric criteria, one call per principle."""

import json
import logging
import re
import uuid
from typing import Any

from app.ai import blocks
from app.ai.evidence import evidence_found
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
- "Compliant": the syllabus clearly satisfies the criterion. Cite the passage that demonstrates this.
- "Partially compliant": addressed, but incompletely or ambiguously.
- "Not compliant": missing or contradicted. {missing_rule}
- "Cannot verify": {cannot_verify_rule}
For every criterion write:
- "evidence": copied verbatim from the syllabus: one short passage, at most 200 characters. Never paraphrase.
  When something is missing, quote the closest related passage (for example the outcome that is not assessed).
- "explanation": why the evidence leads to the given status, in one short sentence. Leave it empty ("") for
  Compliant: the quote shows it.
- "recommendation": one concrete edit the professor can make, in one sentence. Leave it empty ("") for Compliant.
- Only explanation and recommendation are left empty for Compliant: every criterion, Compliant included, still
  needs its quote from the syllabus.
- Write explanation and recommendation in {language}. Keep evidence in the syllabus's original language.
- Judge only what is written. Do not assume content that is not in the document.
- Be brief: the answer is read by software, not by people."""


def cites_blocks(content: ExtractedContent) -> bool:
    """The explanation step cites numbered lines (EXPLAIN_BLOCKS) when the syllabus has been split into them."""
    return get_settings().explain_blocks and bool(content.blocks)


# With numbered lines the model names the line and copies a few words from it; the quote is taken from the line.
# The words come first and are named "quote", not "evidence": asked for "evidence" after a line number, the model
# often wrote the number again in place of the words.
EXPLAIN_BLOCK_EVIDENCE = """- "quote": the words of the syllabus that support the status, copied character for character from ONE line,
  at most 200 characters, without the [number·category] tag. Words, never a line number. When something is
  missing, quote the closest related passage (for example the outcome that is not assessed).
- "line": the number in brackets of the line the quote is copied from."""
_TAG = re.compile(r"^\s*\[\d+·[a-z_]+\]\s*")
_EVIDENCE_RULE = """- "evidence": copied verbatim from the syllabus: one short passage, at most 200 characters. Never paraphrase.
  When something is missing, quote the closest related passage (for example the outcome that is not assessed)."""


def explain_system_prompt(numbered: bool) -> str:
    prompt = EXPLAIN_SYSTEM_PROMPT.format(
        language=get_settings().llm_output_language,
        missing_rule=MISSING_RULE,
        cannot_verify_rule=CANNOT_VERIFY_RULE,
    )
    return prompt.replace(_EVIDENCE_RULE, EXPLAIN_BLOCK_EVIDENCE) if numbered else prompt


def quote_from_block(item: dict[str, Any], content: ExtractedContent) -> tuple[str, int | None]:
    """(evidence, block number) for an answer that names a line: the copied words when they are in that line,
    else the line's start. An answer naming no existing line keeps its copied words, checked like any quote."""
    phrase = _TAG.sub("", item.get("quote") or "").strip()
    if phrase.isdigit():  # a line number written where the words belong
        phrase = ""
    number = item.get("line")
    if not isinstance(number, int) or not 1 <= number <= len(content.blocks):
        return phrase, None
    line = content.blocks[number - 1].text
    if phrase and evidence_found(phrase, line, get_settings().evidence_match_threshold):
        return phrase, number
    return (line if len(line) <= 200 else line[:200].rsplit(" ", 1)[0] + " …"), number


def explain_schema(codes: list[str], numbered: bool = False) -> dict[str, Any]:
    if numbered:
        schema = explain_schema(codes)
        item = schema["properties"]["results"]["items"]
        item["properties"] = {
            "criterion_id": item["properties"]["criterion_id"],
            "quote": {"type": "string"},
            "line": {"type": "integer"},
            "explanation": {"type": "string"},
            "recommendation": {"type": "string"},
        }
        item["required"] = list(item["properties"])
        return schema
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


# The structure findings (checks.technical) count what the extraction found. Syllabi differ in layout, and an
# outcome or weight the extraction missed would turn into a false finding if the models took these as fact.
STRUCTURE_HINTS = (
    "Hints from an automatic reading of the structure (it can miss content in tables or unusual layouts; "
    "confirm each against the syllabus text before relying on it):\n"
)


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
    if statuses is not None and cites_blocks(content):
        numbered = blocks.render(content.blocks, max_document_chars())
        parts = [f"Syllabus, one numbered line each, [number·category] text:\n<<<\n{numbered}\n>>>"]
    else:
        parts = [f"Syllabus:\n<<<\n{document_text(content)}\n>>>"]
    if report.issues:
        parts.append(STRUCTURE_HINTS + "\n".join(f"- {i.message}" for i in report.issues))
    for name in sorted(
        {s.required_context for s in specs if s.required_context and s.required_context in context}
    ):
        parts.append(f"{name}:\n{context[name]}")
    principles = sorted({(s.principle, s.principle_name) for s in specs})
    if len(principles) == 1:
        parts.append(f"Principle {specs[0].principle}: {specs[0].principle_name}")
    else:  # one request for several principles (EXPLAIN_CALLS, or the "single" check mode)
        parts.append("Principles:\n" + "\n".join(f"{n}. {name}" for n, name in principles))
        for item, spec in zip(criteria, specs, strict=True):
            item["principle"] = str(spec.principle)
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
    numbered = cites_blocks(content)
    data = await llm.complete_json(
        purpose="explain",
        system=explain_system_prompt(numbered),
        user=build_user_prompt(specs, content, report, context, statuses),
        schema=explain_schema(codes, numbered),
        meta={
            "criteria": specs,
            "statuses": statuses,
            "content": content,
            "report": report,
            "context": context,
        },
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
        evidence, block = quote_from_block(item, content) if numbered else ((item.get("evidence") or "").strip(), None)
        status = statuses[code]
        if not evidence and status != ResultStatus.cannot_verify:
            out.append(
                CriterionResultData(
                    criterion_id=code,
                    status=ResultStatus.cannot_verify,
                    evidence="",
                    explanation="The evaluator returned no supporting passage, so its decision could not be verified.",
                    recommendation="Run the check again to obtain evidence for this criterion.",
                    confidence=None,
                    evidence_verified=False,
                )
            )
            continue
        out.append(
            CriterionResultData(
                criterion_id=code,
                status=status,
                evidence=evidence,
                evidence_block=block,
                # A Compliant finding is shown with its quote and a fixed text of the app; the model writes none.
                explanation=""
                if status == ResultStatus.compliant
                else (item.get("explanation") or "").strip()
                or "The evaluator returned no explanation for this criterion.",
                recommendation=NO_CHANGE
                if status == ResultStatus.compliant
                else (item.get("recommendation") or "").strip(),
                confidence=confidences.get(code),
            )
        )
    return out
