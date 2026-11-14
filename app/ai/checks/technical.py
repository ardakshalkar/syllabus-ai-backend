"""Deterministic checks: no LLM involved. Produces the technical report and evaluates `deterministic` criteria."""

from collections.abc import Callable

from app.ai.types import CriterionResultData, CriterionSpec, ExtractedContent, TechnicalIssue, TechnicalReport
from app.enums import ResultStatus

REQUIRED_SECTIONS = ("outcomes", "content", "assessment", "resources", "policy")
SECTION_LABELS = {
    "outcomes": "Learning outcomes",
    "content": "Content / weekly plan",
    "assessment": "Assessment",
    "resources": "Resources",
    "policy": "Course policy",
}


def build_report(content: ExtractedContent) -> TechnicalReport:
    report = TechnicalReport()
    report.missing_sections = [s for s in REQUIRED_SECTIONS if s not in content.sections]
    for s in report.missing_sections:
        report.issues.append(
            TechnicalIssue(code="missing_section", message=f"Section not found: {SECTION_LABELS[s]}")
        )

    weights = [a.weight for a in content.assessments if a.weight is not None]
    report.weights_total = round(sum(weights), 2) if weights else None
    if report.weights_total is not None and abs(report.weights_total - 100) > 0.5:
        report.issues.append(
            TechnicalIssue(
                code="weights_total", message=f"Assessment weights total {report.weights_total:g}%"
            )
        )

    assessed = {code for a in content.assessments for code in a.outcomes}
    # Only a syllabus whose assessment names outcomes at all can leave one out. Many link outcomes to tasks
    # elsewhere (in the weekly plan, in prose) or not by code, and would otherwise "miss" every outcome.
    report.unassessed_outcomes = [o.code for o in content.outcomes if assessed and o.code not in assessed]
    for code in report.unassessed_outcomes:
        report.issues.append(
            TechnicalIssue(code="unassessed_outcome", message=f"{code} is not mapped to an assessment")
        )
    if not content.outcomes:
        report.issues.append(
            TechnicalIssue(code="no_outcomes", message="No learning outcomes were found by the automatic reading")
        )
    return report


RuleResult = tuple[ResultStatus, str, str, str]
Rule = Callable[[ExtractedContent, TechnicalReport], RuleResult]


def _outcomes_numbered(content: ExtractedContent, report: TechnicalReport) -> RuleResult:
    if len(content.outcomes) >= 2:
        return (
            ResultStatus.compliant,
            content.outcomes[0].text,
            f"{len(content.outcomes)} numbered learning outcomes were found.",
            "No change suggested for this criterion.",
        )
    if content.outcomes:
        return (
            ResultStatus.partial,
            content.outcomes[0].text,
            "Only one numbered learning outcome was found.",
            "List each learning outcome separately as LO1, LO2, ….",
        )
    return (
        ResultStatus.not_compliant,
        content.sections.get("outcomes", "")[:300],
        "No numbered learning outcomes were found.",
        "Add a Learning Outcomes section with outcomes numbered LO1, LO2, ….",
    )


def _weights_total_100(content: ExtractedContent, report: TechnicalReport) -> RuleResult:
    first = content.assessments[0].text if content.assessments else ""
    if report.weights_total is None:
        return (
            ResultStatus.not_compliant,
            first,
            "No weighted assessment tasks were found.",
            "State the weight of every assessment task as a percentage.",
        )
    if abs(report.weights_total - 100) <= 0.5:
        return (
            ResultStatus.compliant,
            first,
            "Assessment weights total 100%.",
            "No change suggested for this criterion.",
        )
    return (
        ResultStatus.not_compliant,
        first,
        f"Assessment weights total {report.weights_total:g}%, not 100%.",
        "Adjust the weights so they total exactly 100%.",
    )


def _required_sections_present(content: ExtractedContent, report: TechnicalReport) -> RuleResult:
    evidence = content.headings[0] if content.headings else ""
    missing = [SECTION_LABELS[s] for s in report.missing_sections]
    if not missing:
        return (
            ResultStatus.compliant,
            evidence,
            "All required sections are present with labeled headings.",
            "No change suggested for this criterion.",
        )
    status = ResultStatus.partial if len(missing) == 1 else ResultStatus.not_compliant
    return (
        status,
        evidence,
        f"Missing sections: {', '.join(missing)}.",
        "Add the missing sections under clear headings.",
    )


RULES: dict[str, Rule] = {
    "outcomes_numbered": _outcomes_numbered,
    "weights_total_100": _weights_total_100,
    "required_sections_present": _required_sections_present,
}


def evaluate_rule(
    spec: CriterionSpec, content: ExtractedContent, report: TechnicalReport
) -> CriterionResultData:
    assert spec.rule is not None
    status, evidence, explanation, recommendation = RULES[spec.rule](content, report)
    return CriterionResultData(
        criterion_id=spec.code,
        status=status,
        evidence=evidence,
        explanation=explanation,
        recommendation=recommendation,
        confidence=1.0,
    )
