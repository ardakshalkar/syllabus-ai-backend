"""Read a syllabus's structure (outcomes, assessment, weeks, sections) from its Markdown with an LLM.

Replaces the regular expressions in `extraction.parse_lines`, which only see outcomes written as "LO1 …".
The model returns verbatim text; anything it returns that is not in the document is dropped, so it cannot
invent outcomes or tasks. Counting and arithmetic (how many outcomes, whether weights total 100) stay in code
(`checks.technical`), working on what this returns.
"""

import asyncio
import logging
import re
import uuid
from pathlib import Path
from typing import Any

from rapidfuzz import fuzz

from app.ai.evidence import normalize
from app.ai.extraction import detect_language, extract, parse_lines
from app.ai.llm import LLMProvider
from app.ai.types import AssessmentItem, ExtractedContent, Outcome, Week
from app.config import get_settings

log = logging.getLogger(__name__)

# Stored with each cached extraction; bump it when this module changes what it produces, so cached ones are redone.
EXTRACTOR = "markdown-llm-1"
LINES = "lines-2"  # "lines" (before 2026-11-14) skipped tables nested in table cells


def wanted_extractor() -> str:
    s = get_settings()
    return EXTRACTOR if s.structure_extraction and s.llm_provider != "fake" else LINES


async def read_syllabus(path: Path, llm: LLMProvider) -> tuple[ExtractedContent, str]:
    """The syllabus's content and which extractor produced it. A file that cannot be opened raises
    ExtractionError as before; if only the Markdown or the model fails, the line extraction is used."""
    base = await asyncio.to_thread(extract, path)
    if wanted_extractor() != EXTRACTOR:
        return base, LINES
    try:
        from app.ai.markdown import to_markdown

        markdown = await asyncio.to_thread(to_markdown, path)
        content, report = await extract_structure(markdown, llm, base=base)
    except Exception:
        log.exception("structure extraction failed for %s; using the line extraction", path.name)
        return base, LINES
    if report["dropped"]:
        log.info("structure extraction dropped text not in the document: %s", report["dropped"])
    return content, EXTRACTOR


SECTION_KEYS = ("purpose", "outcomes", "content", "assessment", "resources", "policy", "methods")

SYSTEM = """You read a university course syllabus written in Markdown and return its structure as JSON.
Copy text VERBATIM from the document: do not translate, paraphrase, fix or merge it.

- outcomes: every learning outcome the course states (the block may be titled "Learning outcomes",
  "Результаты обучения", "Оқыту нәтижелері", or similar). One entry per outcome, whether it is numbered, a
  list item, or a separate line/paragraph. A single sentence stays ONE outcome even if it lists several skills
  ("will be able to do A, B and C"); never split a sentence. `code` is the label as written (e.g. "LO1", "РО 2") or null.
  Do not include the lead-in sentence ("Upon completion of the course, students:") or program-level outcomes.
- outcomes_heading: the heading or label of that block, verbatim, or null if there is none.
- assessments: the graded components, one entry per row of the assessment table. `name` = the text of ONE
  cell (the assignment title cell), verbatim; never join cells. `weight_percent` = the share of the FINAL course
  grade written on that same row (else null). A row that carries a weight towards the final grade (e.g.
  "ISC 1 … 30%", "Final exam … 40%") is a top-level component with `parent` null, even if it is also a single
  task such as a project defense. `parent` is set only for tasks listed inside such a component (e.g. a task
  inside "ISC 1"). `outcomes` = outcome codes the document links to it, else [].
- weeks: the weekly plan, one entry per week, `topic` verbatim.
- sections: for each kind of section present, the heading verbatim. Kinds: purpose (course goal/aim),
  outcomes, content (topics / weekly plan), assessment, resources (literature), policy (course/academic
  policy, attendance, academic honesty), methods (teaching methods)."""

SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "outcomes": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {"code": {"type": ["string", "null"]}, "text": {"type": "string"}},
                "required": ["code", "text"],
            },
        },
        "outcomes_heading": {"type": ["string", "null"]},
        "assessments": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "weight_percent": {"type": ["number", "null"]},
                    "parent": {"type": ["string", "null"]},
                    "outcomes": {"type": "array", "items": {"type": "string"}},
                },
                "required": ["name", "weight_percent", "parent", "outcomes"],
            },
        },
        "weeks": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {"number": {"type": "integer"}, "topic": {"type": "string"}},
                "required": ["number", "topic"],
            },
        },
        "sections": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "kind": {"type": "string", "enum": list(SECTION_KEYS)},
                    "heading": {"type": "string"},
                },
                "required": ["kind", "heading"],
            },
        },
    },
    "required": ["outcomes", "outcomes_heading", "assessments", "weeks", "sections"],
}

_MD = re.compile(r"^\s*(?:[->]|\d+\.)\s", re.MULTILINE)  # list and quote markers; the rest is in normalize
_CODE = re.compile(r"(?:LO|РО|ОН|CLO|PLO)\s*-?\s*(\d+)", re.IGNORECASE)


def _plain(text: str) -> str:
    return normalize(_MD.sub(" ", text))


def in_document(snippet: str, haystack: str, threshold: int = 90) -> bool:
    needle = _plain(snippet).rstrip(".,;…")
    if len(needle) < 3:
        return False
    return needle in haystack or fuzz.partial_ratio(needle, haystack) >= threshold


def name_in_document(name: str, haystack: str) -> bool:
    """A component name may join table cells ("Final exam: Project defense"); each part must be there."""
    if in_document(name, haystack):
        return True
    parts = [p for p in re.split(r"[:›—–]|\s-\s", name) if len(p.strip()) >= 3]
    return len(parts) > 1 and all(in_document(p, haystack) for p in parts)


def _code(raw: str | None, index: int) -> str:
    if raw and (m := _CODE.search(raw)):
        return f"LO{int(m.group(1))}"
    return f"LO{index}"


async def extract_structure(
    markdown: str,
    llm: LLMProvider,
    *,
    base: ExtractedContent | None = None,
    run_id: uuid.UUID | None = None,
    trace: bool = True,
) -> tuple[ExtractedContent, dict[str, Any]]:
    """The content the pipeline works on, and a small report of what the model returned and what was dropped.

    `raw_text` (what the models read and evidence is checked against) is the Markdown. `paragraphs` stay those of
    the line extraction (`base`) when given: the version diff and applying proposals match against them."""
    data = await llm.complete_json(
        purpose="structure", system=SYSTEM, user=markdown, schema=SCHEMA, run_id=run_id, trace=trace
    )
    haystack = _plain(markdown)
    dropped: list[str] = []

    outcomes: list[Outcome] = []
    for i, o in enumerate(data.get("outcomes", []), start=1):
        text = " ".join(str(o.get("text", "")).split())
        if not in_document(text, haystack):
            dropped.append(f"outcome: {text[:80]}")
            continue
        code = _code(o.get("code"), i)
        if code in {x.code for x in outcomes}:
            code = f"LO{len(outcomes) + 1}"
        outcomes.append(Outcome(code=code, text=text))

    assessments: list[AssessmentItem] = []
    for a in data.get("assessments", []):
        name = " ".join(str(a.get("name", "")).split())
        if not name_in_document(name, haystack):
            dropped.append(f"assessment: {name[:80]}")
            continue
        weight = a.get("weight_percent")
        # Only top-level components carry a share of the final grade; tasks inside one must not add to it.
        if a.get("parent"):
            weight = None
        refs = sorted(
            {_code(r, 0) for r in a.get("outcomes", []) if _CODE.search(r)}, key=lambda c: int(c[2:])
        )
        label = f"{a['parent']} › {name}" if a.get("parent") else name
        assessments.append(
            AssessmentItem(text=label, weight=float(weight) if weight is not None else None, outcomes=refs)
        )

    weeks = [
        Week(number=int(w["number"]), text=" ".join(str(w["topic"]).split()))
        for w in data.get("weeks", [])
        if in_document(str(w.get("topic", "")), haystack)
    ]

    # Sections: the headings the model found, kept only if they really are in the document; their body is
    # whatever follows the heading up to the next heading of the same or higher level.
    lines = markdown.splitlines()
    sections: dict[str, str] = {}
    headings: list[str] = []
    for s in data.get("sections", []):
        kind, heading = s.get("kind"), " ".join(str(s.get("heading", "")).split())
        if kind not in SECTION_KEYS or kind in sections or not in_document(heading, haystack):
            continue
        headings.append(heading)
        sections[kind] = _section_body(lines, heading)
    if "outcomes" not in sections and outcomes:
        sections["outcomes"] = "\n".join(o.text for o in outcomes)

    if base is None:
        base = parse_lines(
            [(line.lstrip("#").strip(), line.startswith("#")) for line in lines if line.strip()]
        )
    content = ExtractedContent(
        language=detect_language(markdown),
        raw_text=markdown,
        paragraphs=base.paragraphs,
        headings=headings or base.headings,
        sections={**base.sections, **sections},
        outcomes=outcomes,
        weeks=weeks,
        assessments=assessments,
        resources=sections.get("resources", "").splitlines() or base.resources,
    )
    report = {
        "outcomes_heading": data.get("outcomes_heading"),
        "returned": {k: len(data.get(k, [])) for k in ("outcomes", "assessments", "weeks", "sections")},
        "dropped": dropped,
    }
    return content, report


def _section_body(lines: list[str], heading: str) -> str:
    target = _plain(heading)
    # Narxoz template: "| label | value |" — the body is the rest of the row.
    for line in lines:
        cells = [c.strip() for c in line.strip().strip("|").split(" | ")]
        if line.lstrip().startswith("|") and len(cells) >= 2 and _plain(cells[0]) == target:
            return " | ".join(cells[1:])
    for i, line in enumerate(lines):
        if _plain(line) == target or (line.startswith("#") and target in _plain(line)):
            level = len(line) - len(line.lstrip("#")) or 9
            body = []
            for nxt in lines[i + 1 :]:
                n = len(nxt) - len(nxt.lstrip("#"))
                if nxt.startswith("#") and n <= level:
                    break
                body.append(nxt)
            return "\n".join(body).strip()
    return ""
