"""Deterministic stand-in for the LLM (LLM_PROVIDER=fake), used by tests and GPU-less development.

It applies a few heuristics modelled on the prototype's sample review (criteria 1.2, 3.5, 4.3, 6.1, 6.4, 9.2)
and quotes real paragraphs, so evidence verification and the improve → re-check loop behave like production.
"""

import re
import uuid
from typing import Any

from app.ai.types import CriterionResultData, CriterionSpec, ExtractedContent, TechnicalReport
from app.enums import ResultStatus

_VAGUE_VERBS = re.compile(r"\b(understand|know|be familiar|понимать|знать|түсіну|білу)\w*", re.IGNORECASE)
_LO_REF = re.compile(r"\b(?:LO|РО|ОН)\s*\d", re.IGNORECASE)
_SECTION_FOR_PRINCIPLE = {
    1: "content",
    2: "academic_context",
    3: "outcomes",
    4: "content",
    5: "methods",
    6: "assessment",
    7: "resources",
    8: "course_info",
    9: "policy",
}


def _first_line(content: ExtractedContent, section: str | None) -> str:
    text = content.sections.get(section or "", "")
    for line in text.splitlines():
        if len(line.strip()) >= 8:
            return line.strip()
    return next((p for p in content.paragraphs if len(p) >= 8), content.paragraphs[0])


def _result(code: str, status: ResultStatus, evidence: str, explanation: str, recommendation: str) -> dict:
    confidence = 0.92 if status == ResultStatus.compliant else 0.81
    return {
        "criterion_id": code,
        "status": status.value,
        "evidence": evidence,
        "explanation": explanation,
        "recommendation": recommendation,
        "confidence": confidence,
    }


def _judge(spec: CriterionSpec, content: ExtractedContent, report: TechnicalReport) -> dict:
    code = spec.code
    partial, failed = ResultStatus.partial, ResultStatus.not_compliant
    if code == "1.2":
        unmapped = [w for w in content.weeks if not _LO_REF.search(w.text)]
        if unmapped and len(unmapped) < len(content.weeks):
            return _result(
                code,
                partial,
                unmapped[0].text,
                "One section has no visible connection to a course outcome.",
                f"Map Week {unmapped[0].number} activities to the relevant learning outcome.",
            )
    if code == "3.5":
        vague = [o for o in content.outcomes if _VAGUE_VERBS.search(o.text)]
        if vague:
            return _result(
                code,
                partial,
                vague[0].text,
                "The outcome uses a verb that does not describe a directly observable or measurable action.",
                "Use an observable action such as compare, implement, evaluate, justify, or design.",
            )
    if code == "4.3":
        weeks_text = " ".join(w.text.lower() for w in content.weeks)
        for o in content.outcomes:
            if "deploy" in o.text.lower() and "deploy" not in weeks_text and content.weeks:
                return _result(
                    code,
                    partial,
                    o.text,
                    "The content plan does not show where deployment is taught.",
                    "Add a deployment topic or revise the outcome to match the taught content.",
                )
    if code == "6.1" and report.unassessed_outcomes and content.assessments:
        missing = ", ".join(report.unassessed_outcomes)
        return _result(
            code,
            failed,
            content.assessments[-1].text,
            f"The assessment table does not map any task to {missing}.",
            f"Map {missing} to an existing assessment or add an activity that verifies it.",
        )
    if code == "6.4":
        text = content.sections.get("assessment", "").lower()
        if text and not re.search(r"level|descriptor|excellent|уровн|деңгей", text):
            lines = content.sections["assessment"].splitlines()
            described = next((ln for ln in lines if re.search(r"assessed|rubric|criteria", ln, re.I)), None)
            return _result(
                code,
                partial,
                described or _first_line(content, "assessment"),
                "Students cannot see how work quality affects the mark.",
                "Add criterion level descriptors to the project rubric.",
            )
    if code == "9.2":
        text = content.sections.get("policy", "")
        if re.search(r"respect|уважен|құрмет", text, re.IGNORECASE) and not re.search(
            r"responsib|contribution|ответствен|жауапкершілік", text, re.IGNORECASE
        ):
            line = next(
                ln for ln in text.splitlines() if re.search(r"respect|уважен|құрмет", ln, re.IGNORECASE)
            )
            return _result(
                code,
                partial,
                line,
                "Expected conduct in collaborative tasks is incomplete.",
                "Clarify individual responsibility and respectful conduct in group work.",
            )
    return _result(
        code,
        ResultStatus.compliant,
        _first_line(content, _SECTION_FOR_PRINCIPLE.get(spec.principle)),
        f"The cited syllabus section provides support for this criterion: {spec.name.lower()}.",
        "No change suggested for this criterion.",
    )


def _propose(result: CriterionResultData, content: ExtractedContent) -> dict | None:
    current = result.evidence
    if not current:
        return None
    code = result.criterion_id
    if code == "3.5":
        proposed = _VAGUE_VERBS.sub("compare", current, count=1).rstrip(".") + " and justify their selection."
        title, reason = (
            "Make outcome observable",
            "This makes the learning outcome observable and assessable.",
        )
    elif code == "6.1":
        missing = re.findall(r"LO\d+", result.explanation)
        proposed = current.rstrip(".") + ", " + ", ".join(missing)
        title, reason = (
            "Assess every outcome",
            "Every learning outcome needs evidence from an assessment activity.",
        )
    elif code == "6.4":
        proposed = (
            current.rstrip(".") + ". Performance levels: excellent, satisfactory and insufficient work."
        )
        title, reason = (
            "Clarify grading levels",
            "Performance level descriptors make grading expectations transparent.",
        )
    elif code == "9.2":
        proposed = (
            "In group work, each student documents their individual contribution and responsibility, "
            "and communicates respectfully with peers."
        )
        title, reason = (
            "Clarify group responsibility",
            "This states each student's responsibility in group work.",
        )
    elif code == "4.3":
        # The evidence is the outcome that is never taught; add its topic to the last week and map it.
        outcome = re.match(r"LO\d+", current)
        last = content.weeks[-1].text if content.weeks else current
        mapped = f" ({outcome.group(0)})" if outcome else ""
        current, proposed = last, last.rstrip(".") + f" and deployment considerations{mapped}."
        title, reason = "Cover deployment", "The content plan will then address the outcome."
    elif code == "1.2":
        first_lo = content.outcomes[0].code if content.outcomes else "LO1"
        proposed = current.rstrip(".") + f" (supports {first_lo})."
        title, reason = "Map week to an outcome", "The week becomes traceable to a learning outcome."
    else:
        proposed = current.rstrip(".") + f". {result.recommendation}"
        title, reason = "Apply recommendation", result.recommendation
    return {"criterion_id": code, "title": title, "current": current, "proposed": proposed, "reason": reason}


_ASSISTANT_REPLIES = {
    "Learning outcomes": "LO1. Compare core approaches to the topic and justify their selection for a given problem.",
    "Assessment": "Midterm exam 30% (LO1, LO2); Project 40% (LO3, LO4); Final exam 30% (LO1–LO5). Total 100%.",
    "Resources": "List two or three core textbooks and check their availability in the Narxoz library catalogue.",
    "Course policies": "State attendance rules, academic integrity expectations, and how students can appeal a grade.",
}


class FakeProvider:
    model_version = "fake-evaluator"

    async def complete_json(
        self,
        *,
        purpose: str,
        system: str,
        user: str,
        schema: dict[str, Any],
        meta: dict[str, Any] | None = None,
        run_id: uuid.UUID | None = None,
        trace: bool = True,
    ) -> dict[str, Any]:
        meta = meta or {}
        if purpose == "criteria":
            return {"results": [_judge(s, meta["content"], meta["report"]) for s in meta["criteria"]]}
        if purpose == "explain":
            # The status was decided elsewhere (a decision model); only the text is written.
            keep = ("criterion_id", "evidence", "explanation", "recommendation")
            judged = [_judge(s, meta["content"], meta["report"]) for s in meta["criteria"]]
            return {"results": [{k: r[k] for k in keep} for r in judged]}
        if purpose == "proposals":
            items = [_propose(r, meta["content"]) for r in meta["failing"]]
            return {"proposals": [i for i in items if i]}
        raise ValueError(f"fake provider has no behaviour for '{purpose}'")

    async def complete_text(
        self,
        *,
        purpose: str,
        system: str,
        user: str,
        meta: dict[str, Any] | None = None,
        trace: bool = True,
    ) -> str:
        section = (meta or {}).get("section", "")
        return _ASSISTANT_REPLIES.get(
            section, f"Draft the {section.lower()} section in clear academic language."
        )
