"""Guard against invented quotes: evidence must actually appear in the document."""

import re

from rapidfuzz import fuzz

from app.ai.types import CriterionResultData
from app.enums import ResultStatus

_QUOTES = str.maketrans("", "", "\"'«»“”„‘’`")
# Markdown markup (the document text is Markdown when structure extraction is on): table pipes and rules,
# emphasis, heading marks, escapes, underline and line-break tags. A quote may run across table cells.
_MARKUP = re.compile(r"\|\s*-{3,}\s*|[|*#\\]|</?u>|<br\s*/?>")


def normalize(text: str) -> str:
    return re.sub(r"\s+", " ", _MARKUP.sub(" ", text).translate(_QUOTES)).strip().lower()


_ELISION = re.compile(r"\.{3,}|…|\[[^\]]*\]")  # "first part ... second part", "[and other outcomes]"


def _found(needle: str, haystack: str, threshold: int) -> bool:
    return needle in haystack or fuzz.partial_ratio(needle, haystack) >= threshold


def evidence_found(evidence: str, document: str, threshold: int) -> bool:
    """The quote is in the document; a quote shortened with "..." counts if every fragment of it is there."""
    haystack = normalize(document)
    fragments = [f.strip(" .,;:") for f in _ELISION.split(normalize(evidence))]
    fragments = [f for f in fragments if f]
    if not fragments or sum(len(f) for f in fragments) < 8:
        return False
    # Fragments too short to tell anything (a lone number, "1.") are skipped rather than matched loosely.
    meaningful = [f for f in fragments if len(f) >= 8] or fragments
    return all(_found(f, haystack, threshold) for f in meaningful)


def verify(result: CriterionResultData, document: str, threshold: int) -> CriterionResultData:
    """Downgrade a decided status to "Cannot verify" when its evidence is not in the document."""
    if result.status == ResultStatus.cannot_verify or not result.evidence:
        return result
    if evidence_found(result.evidence, document, threshold):
        return result.model_copy(update={"evidence_verified": True})
    return result.model_copy(
        update={
            "status": ResultStatus.cannot_verify,
            "evidence_verified": False,
            "confidence": None,
            "explanation": "The cited evidence could not be found in the document, so the evaluator's "
            f"status ({result.status.value}) was not accepted. {result.explanation}",
        }
    )


def recover_table_quote(result: CriterionResultData, document: str) -> CriterionResultData:
    """Mark omissions between separately quoted cells explicitly, only when each label is in the source.

    A model may join a column header and a distant cell with a pipe. Treating that as one contiguous
    quote loses an otherwise grounded finding. Do not accept invented labels or short ambiguous values.
    """
    if "|" not in result.evidence or evidence_found(result.evidence, document, 100):
        return result
    parts = [p.strip() for p in result.evidence.split("|") if p.strip()]
    source = normalize(document)
    if len(parts) < 2 or any(len(normalize(p)) < 8 or normalize(p) not in source for p in parts):
        return result
    evidence = "\n...\n".join(parts)
    return result.model_copy(update={"evidence": evidence})
