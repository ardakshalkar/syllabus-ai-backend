"""Turn an uploaded .docx/.pdf into ExtractedContent: paragraphs, sections, outcomes, weeks, assessments."""

import re
from collections.abc import Iterator
from pathlib import Path
from typing import TYPE_CHECKING

from app.ai.types import AssessmentItem, ExtractedContent, Outcome, Week

if TYPE_CHECKING:
    from docx.table import Table, _Cell


class ExtractionError(Exception):
    """The file cannot be opened or holds no text (prototype status "Cannot open")."""


class NoTextError(ExtractionError):
    """The file opens but has no text layer, e.g. a scanned PDF; app.ai.ocr may still read it."""


SUPPORTED_TYPES = {
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ".pdf": "application/pdf",
}

# Section keys used across the pipeline. Keywords cover English, Russian and Kazakh headings.
SECTION_KEYWORDS: dict[str, tuple[str, ...]] = {
    "course_info": ("course information", "информация о курсе", "курс туралы"),
    "purpose": (
        "course purpose",
        "course goal",
        "purpose",
        "цель курса",
        "цель дисциплины",
        "курстың мақсаты",
    ),
    "academic_context": ("academic context", "prerequisite", "пререквизит", "академический контекст"),
    "outcomes": ("learning outcomes", "результаты обучения", "оқыту нәтижелері", "оқу нәтижелері"),
    "content": ("content", "weekly plan", "course schedule", "тематический план", "содержание", "мазмұны"),
    "methods": ("teaching methods", "методы обучения", "оқыту әдістері"),
    "assessment": ("assessment", "оценивание", "система оценки", "бағалау"),
    "resources": ("resources", "literature", "reading list", "литература", "әдебиет"),
    "policy": ("course policy", "policies", "policy", "политика", "саясаты"),
}

_OUTCOME_RE = re.compile(r"^\s*(?:LO|РО|ОН)\s*-?\s*(\d+)\s*[.:)\-–—]?\s*(.+)$", re.IGNORECASE)
_OUTCOME_REF_RE = re.compile(r"(?:LO|РО|ОН)\s*-?\s*(\d+)", re.IGNORECASE)
_WEEK_RE = re.compile(r"^\s*(?:Week|Неделя|Апта)\s*(\d+)\s*[.:)\-–—]?\s*(.+)$", re.IGNORECASE)
_WEIGHT_RE = re.compile(r"(\d+(?:[.,]\d+)?)\s*%")
_KAZAKH_CHARS = set("әғқңөұүһі")


def detect_language(text: str) -> str:
    lowered = text.lower()
    if any(ch in _KAZAKH_CHARS for ch in lowered):
        return "Kazakh"
    cyrillic = sum(1 for ch in lowered if "а" <= ch <= "я" or ch == "ё")
    latin = sum(1 for ch in lowered if "a" <= ch <= "z")
    return "Russian" if cyrillic > latin else "English"


def _section_for_heading(text: str) -> str | None:
    lowered = text.lower().strip().rstrip(":")
    if len(lowered) > 80:
        return None
    for key, words in SECTION_KEYWORDS.items():
        if any(lowered.startswith(w) or lowered == w for w in words):
            return key
    return None


def iter_rows(table: "Table") -> "Iterator[list[_Cell]]":
    """Every row of a table, then the rows of the tables nested in its cells, in document order.

    `cell.text` holds only a cell's own paragraphs, so a table nested in a cell (the Narxoz template keeps the
    weekly plan and the assessment table inside its outer two-column table) must be walked separately."""
    for row in table.rows:
        cells = list(row.cells)
        yield cells
        seen: list[object] = []
        for cell in cells:
            if cell._tc in seen:  # a merged cell appears once per grid column it spans
                continue
            seen.append(cell._tc)
            for nested in cell.tables:
                yield from iter_rows(nested)


def _read_docx(path: Path) -> list[tuple[str, bool]]:
    """(text, is_heading) in document order; table rows, nested ones included, are joined with ' | '."""
    import docx
    from docx.table import Table
    from docx.text.paragraph import Paragraph

    try:
        document = docx.Document(str(path))
    except Exception as exc:  # python-docx raises several unrelated types for corrupt files
        raise ExtractionError(f"cannot open .docx: {exc}") from exc
    lines: list[tuple[str, bool]] = []
    for block in document.iter_inner_content():
        if isinstance(block, Paragraph):
            text = block.text.strip()
            if text:
                style = (block.style.name if block.style is not None else "") or ""
                lines.append((text, style.lower().startswith(("heading", "title"))))
        elif isinstance(block, Table):
            for row in iter_rows(block):
                cells = [c.text.strip() for c in row]
                unique = [c for i, c in enumerate(cells) if c and c not in cells[:i]]
                if unique:
                    lines.append((" | ".join(unique), False))
    return lines


def _read_pdf(path: Path) -> list[tuple[str, bool]]:
    import pdfplumber

    try:
        with pdfplumber.open(str(path)) as pdf:
            text = "\n".join(page.extract_text() or "" for page in pdf.pages)
    except Exception as exc:
        raise ExtractionError(f"cannot open .pdf: {exc}") from exc
    return [(line.strip(), False) for line in text.splitlines() if line.strip()]


def extract(path: Path) -> ExtractedContent:
    suffix = path.suffix.lower()
    if suffix == ".docx":
        lines = _read_docx(path)
    elif suffix == ".pdf":
        lines = _read_pdf(path)
    else:
        raise ExtractionError(f"unsupported file type {suffix}; upload .docx or .pdf")
    if not lines:
        raise NoTextError("the document contains no text")
    return parse_lines(lines)


def parse_lines(lines: list[tuple[str, bool]]) -> ExtractedContent:
    paragraphs = [text for text, _ in lines]
    headings: list[str] = []
    sections: dict[str, list[str]] = {}
    current: str | None = None
    for text, is_heading in lines:
        key = _section_for_heading(text)
        if key and (is_heading or len(text) <= 40):
            headings.append(text)
            current = key
            sections.setdefault(key, [])
            continue
        if is_heading:
            headings.append(text)
        if current:
            sections[current].append(text)

    outcomes: list[Outcome] = []
    weeks: list[Week] = []
    for text in paragraphs:
        if m := _OUTCOME_RE.match(text):
            code = f"LO{int(m.group(1))}"
            if code not in {o.code for o in outcomes}:
                outcomes.append(Outcome(code=code, text=text))
        if m := _WEEK_RE.match(text):
            weeks.append(Week(number=int(m.group(1)), text=text))

    assessments: list[AssessmentItem] = []
    for text in sections.get("assessment", []):
        # A task line carries exactly one percentage; several (e.g. "quality 60%, evaluation 25%, ...")
        # describe a breakdown inside one task and must not add to the course total.
        weights = _WEIGHT_RE.findall(text)
        refs = sorted({f"LO{int(n)}" for n in _OUTCOME_REF_RE.findall(text)}, key=lambda c: int(c[2:]))
        if len(weights) == 1 or refs:
            assessments.append(
                AssessmentItem(
                    text=text,
                    weight=float(weights[0].replace(",", ".")) if len(weights) == 1 else None,
                    outcomes=refs,
                )
            )

    raw_text = "\n".join(paragraphs)
    return ExtractedContent(
        language=detect_language(raw_text),
        raw_text=raw_text,
        paragraphs=paragraphs,
        headings=headings,
        sections={k: "\n".join(v) for k, v in sections.items()},
        outcomes=outcomes,
        weeks=weeks,
        assessments=assessments,
        resources=sections.get("resources", []),
    )
