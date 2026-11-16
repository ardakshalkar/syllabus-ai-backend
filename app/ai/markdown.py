"""Turn a .docx/.pdf syllabus into Markdown with Microsoft's markitdown (mammoth for .docx, pdfminer for .pdf).

It keeps what the old line extraction lost: Word's automatic list numbering (so outcomes appear as "1. …"),
merged cells, and tables as tables. Narxoz's template is one big two-column table, so most of a syllabus
becomes `| label | value |` rows; the nested tables (weekly plan, assessment) end up flattened inside a cell,
which the structure step reads without trouble.

markitdown is a project dependency (`markitdown[docx,pdf]`).
"""

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
