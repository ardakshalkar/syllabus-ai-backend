"""The place of a quote in the syllabus (QuoteContext), found once a check's evidence is verified.

A quote is a few words, at most 200 characters, and in a syllabus most of the text sits in table rows: OCR writes
a row of the template on one Markdown line, often with the goal, the outcomes and the assessment side by side.
Shown alone, "Apply negotiation, mediation…" does not say it is an outcome; with its row and its column's name
it does. A paragraph is given whole, and the screen shortens it around the quote.
"""

import re

from rapidfuzz import fuzz

from app.ai.evidence import normalize
from app.ai.types import QuoteContext

_RULE = re.compile(r"^\s*\|?[\s:|-]+\|?\s*$")
_ELISION = re.compile(r"\.{3,}|…|\n")
_BREAK = re.compile(r"<br\s*/?>", re.I)
# A row's cell may hold a whole nested table or list; kept to this many characters around the quote.
MAX_CELL = 1200
FUZZY = 85


def _cells(line: str) -> list[str]:
    inner = line.strip()
    inner = inner[1:] if inner.startswith("|") else inner
    inner = inner[:-1] if inner.endswith("|") else inner
    return [_BREAK.sub("\n", c).replace("**", "").strip() for c in inner.split("|")]


def _needle(evidence: str) -> str:
    """The longest fragment of the quote: a quote shortened with "..." is looked up by its biggest part."""
    parts = [normalize(p) for p in _ELISION.split(evidence)]
    return max(parts, key=len, default="")


def _holds(needle: str, text: str) -> float:
    haystack = normalize(text)
    if not needle or not haystack:
        return 0.0
    if needle in haystack:
        return 100.0
    return fuzz.partial_ratio(needle, haystack) if len(haystack) >= len(needle) * 0.6 else 0.0


_DATA = re.compile(r"^[\d.,\s\-–]+$")


def _header(lines: list[str], row: int) -> list[str]:
    """The column names of the table the row is in: its first line, when a header rule follows it and it reads
    as names. Markdown makes every table's first row a header: in a two-column table of the template it is the
    first label and its value ("Course title | Applied Statistics"), and a weekly plan may start with week 1."""
    start = row
    while start > 0 and lines[start - 1].lstrip().startswith("|"):
        start -= 1
    if start == row or start + 1 >= len(lines) or not _RULE.match(lines[start + 1]):
        return []
    names = _cells(lines[start])
    if len([n for n in names if n]) < 3 or any(_DATA.match(n) for n in names if n):
        return []
    return names


def _trim(cell: str, needle: str) -> str:
    if len(cell) <= MAX_CELL:
        return cell
    at = normalize(cell).find(needle)  # the normalised text is about as long, so the place is close enough
    start = max(0, min(at if at >= 0 else 0, len(cell) - MAX_CELL) - MAX_CELL // 3)
    piece = cell[start : start + MAX_CELL]
    return ("… " if start else "") + piece + (" …" if start + MAX_CELL < len(cell) else "")


def context_of(evidence: str, document: str) -> QuoteContext | None:
    """The row or paragraph of `document` (Markdown) that holds the quote, or None when no line does."""
    needle = _needle(evidence)
    if len(needle) < 3:
        return None
    lines = document.splitlines()  # blank lines kept: they end a table
    scored = [
        (_holds(needle, line), i) for i, line in enumerate(lines) if line.strip() and not _RULE.match(line)
    ]
    best, row = max(scored, default=(0.0, -1))
    if best < FUZZY:
        return None
    line = lines[row]
    if "|" not in line:
        return QuoteContext(text=line.replace("*", "").strip())
    cells, header = _cells(line), _header(lines, row)
    if header == cells or len(header) != len(cells):
        header = []
    keep = [i for i, c in enumerate(cells) if c]
    cells = [cells[i] for i in keep]
    # A row of one filled cell spans the table (merged cells): the column name over it would mislead.
    header = [header[i] for i in keep] if header and len(keep) > 1 else []
    # Ties go to the first cell, which is the row's label in the template's two-column rows.
    cell = max(range(len(cells)), key=lambda i: (_holds(needle, cells[i]), -i), default=None)
    if cell is not None and _holds(needle, cells[cell]) < FUZZY:
        cell = None
    return QuoteContext(cells=[_trim(c, needle) for c in cells], header=header, cell=cell)
