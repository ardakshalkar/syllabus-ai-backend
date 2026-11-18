"""Turn a .docx/.pdf syllabus into Markdown with Microsoft's markitdown (mammoth for .docx, pdfminer for .pdf).

It keeps what the old line extraction lost: Word's automatic list numbering (so outcomes appear as "1. …"),
merged cells, and tables as tables. Narxoz's template is one big two-column table, so most of a syllabus
becomes `| label | value |` rows; the nested tables (weekly plan, assessment) end up flattened inside a cell,
which the structure step reads without trouble.

markitdown is a project dependency (`markitdown[docx,pdf]`).
"""

import re
from pathlib import Path

from app.ai.extraction import ExtractionError, NoTextError


def to_markdown(path: Path) -> str:
    suffix = path.suffix.lower()
    if suffix not in (".docx", ".pdf"):
        raise ExtractionError(f"unsupported file type {suffix}; upload .docx or .pdf")
    from markitdown import MarkItDown

    try:
        text = MarkItDown(enable_plugins=False).convert(str(path)).text_content
    except Exception as exc:
        raise ExtractionError(f"cannot open {suffix}: {exc}") from exc
    if not text.strip():
        raise NoTextError("the document contains no text")
    return text


_RULE = re.compile(r"^[\s|:-]+$")
# A text layer whose tables are mostly empty cells was laid over a scan by the scanner, not written by Word: its
# letters drift into neighbouring cells ("| N A R \O | Z |"), and words break across them ("Контактны | е").
# In Nov 2026 such PDFs had 47-50% empty cells, against at most 25% in ones exported from Word or read by OCR.
BROKEN_MIN_CELLS = 200
BROKEN_EMPTY_SHARE = 0.4


def looks_broken(markdown: str) -> bool:
    """Whether a PDF's text layer is too damaged to check, so the OCR model should read its pages instead."""
    cells = empty = 0
    for line in markdown.splitlines():
        line = line.strip()
        if not line.startswith("|") or _RULE.match(line):
            continue
        for cell in line.strip("|").split("|"):
            cells += 1
            empty += not cell.strip()
    return cells >= BROKEN_MIN_CELLS and empty / cells >= BROKEN_EMPTY_SHARE
