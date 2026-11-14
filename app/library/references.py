"""Turn a syllabus's literature section into references to look up.

Syllabi cite in GOST ("Мэнкью Н.Г. Принципы экономики. – СПб.: Питер, 2020.") and in APA ("Mankiw, N. G.
(2021). Principles of economics (9th ed.). Cengage."), in Russian, Kazakh and English. Rather than parse every
style, each reference is reduced to its first author's surname and the first words of its title: the catalogue
is then searched for records containing all of them.
"""

import re
from dataclasses import dataclass, field
from typing import Literal

from app.ai.types import ExtractedContent

Kind = Literal["core", "supplementary"]

_CORE = re.compile(r"основн|обязательн|негізгі|міндетті|\bcore\b|required|\bmain\b|essential", re.I)
_SUPPLEMENTARY = re.compile(
    r"дополнит|рекомендуем|қосымша|supplement|additional|further|recommended|optional", re.I
)
_ONLINE_HEADING = re.compile(r"интернет|internet|электронн\w* ресурс|онлайн|online|web", re.I)
_LIST_WORD = re.compile(
    r"литератур|әдебиет|источник|дереккөз|ресурс|reading|literature|sources|resources|textbook|bibliograph",
    re.I,
)
_LITERATURE = re.compile(r"литератур|әдебиет|literature|reading list|readings|bibliograph", re.I)
# The template's left-column label, which the extractor runs into the first words of the right column's lines.
_TEMPLATE_LABEL = re.compile(
    r"(?:required or (?:recommended|supplementary)|literature and other learning|resources/tools"
    r"|обязательная или(?: дополнительная)?|(?:дополнительная )?литература и другие учебные"
    r"|и другие учебные ресурсы|(?:учебные )?ресурсы/инструменты"
    r"|міндетті немесе(?: қосымша)?|(?:ұсынылатын|қосымша) әдебиет және|(?:оқу )?ресурстары/құралдары)\s+",
    re.I,
)
_MAX_SECTION_LINES = 60
_URL = re.compile(r"https?://\S+|www\.\S+", re.I)
# "1." or "12)" but not a year: "2012. ISBN …" continues the reference before it.
_NUMBERING = re.compile(r"^\s*(?:\[\d{1,3}\]|\d{1,3}[.)]|[-–—•·*▪●])\s*")
_ISBN = re.compile(r"ISBN[:\s]*((?:97[89][\s\-–]?)?(?:\d[\s\-–]?){9}[\dXx])", re.I)
_YEAR_PARENS = re.compile(r"\(\s*(?:19|20)\d{2}[a-z]?\s*\)\.?")
# Where the title ends: GOST's " – " area separator, "//" (part of a journal), the edition, a city, or the
# sentence (APA's "Title. Publisher.").
_TITLE_END = re.compile(
    r"\s[–—-]\s|//|\[|\(\s*\d|\.\s|\b\d+(?:-?[ео]|th|nd|rd|st)\b|\b(?:изд|ed|edn|edition|басылым)\b"
    r"|\b(?:М|СПб|Алматы|Астана|Нур-Султан|Москва|London|New York|Boston|Hoboken|Harlow|Oxford|Cambridge)\s*[:.]",
    re.I,
)
_WORD = re.compile(r"[^\W\d_]+(?:[-'][^\W\d_]+)*")
# Title words are searched one by one, and the catalogue splits "Data-Intensive" and "Animator's" into parts.
_TITLE_WORD = re.compile(r"[^\W\d_]+")
# Where the literature section ends when the extractor ran past it into the next part of the syllabus.
_SECTION_END = re.compile(
    r"^\s*(?:planned learning activities|teaching methods|assessment|attendance|language of instruction"
    r"|course policy|academic integrity|grading|планируемые|методы обучения|оценивание|политика|посещаемость"
    r"|язык обучения|жоспарланған|оқыту әдістері|бағалау|саясаты|сабаққа қатысу|оқыту тілі)\b",
    re.I,
)
# Not something a library holds: documentation, standards bodies' web pages, courses, videos.
_NOT_A_BOOK = re.compile(
    r"documentation|документаци|whitepaper|official (?:site|website)|официальн\w* сайт|coursera|udemy|youtube"
    r"|stepik|tutorial|онлайн-курс",
    re.I,
)
# What makes a line a reference rather than prose: a year, an ISBN, an edition, or the GOST separators.
_REFERENCE_MARK = re.compile(
    r"\b(?:19|20)\d{2}\b|ISBN|\s[–—]\s|//|\b(?:edition|ed\.|изд|издание|басылым|учебник|оқулық|textbook)",
    re.I,
)
# A wrapped line that belongs to the reference above: a database export's fields, or "City: Publisher, 2013".
_CONTINUATION = re.compile(
    r"^(?:Авторы|Authors|База данных|Database|Опубликовано|Published)\s*:"
    r"|^[^\W\d_][\w .-]{1,25}:\s*[^\W\d_][^:]*\b(?:19|20)\d{2}\b",
    re.I,
)
STOPWORDS = {
    # English
    "the",
    "and",
    "for",
    "with",
    "from",
    "into",
    "its",
    "their",
    "your",
    "edition",
    "press",
    "publishing",
    "publishers",
    "learning",
    "university",
    "inc",
    "ltd",
    "vol",
    "volume",
    "part",
    "eds",
    "et",
    "al",
    # Russian
    "для",
    "под",
    "при",
    "без",
    "как",
    "или",
    "его",
    "это",
    "изд",
    "издание",
    "издательство",
    "учебник",
    "учебное",
    "пособие",
    "пособия",
    "редакцией",
    "ред",
    "пер",
    "перевод",
    "том",
    "часть",
    "текст",
    # Kazakh
    "және",
    "үшін",
    "оқулық",
    "оқу",
    "құрал",
    "құралы",
    "баспасы",
    "баспа",
    "бет",
    "басылым",
}


@dataclass
class Reference:
    text: str
    kind: Kind
    surname: str | None = None
    title_words: list[str] = field(default_factory=list)
    isbn: str | None = None
    # The first author's names when which one is the surname is unknown ("Skoulikari Anna" or "William
    # Vaughan"): not searched for, but a record must contain one of them.
    name_words: list[str] = field(default_factory=list)
    # What is sent to the catalogue: the title words, a compound by its first part only ("Data-Intensive" ->
    # "Data"), since the catalogue keeps a compound as one word and a stem of its second half finds nothing.
    search_words: list[str] = field(default_factory=list)

    @property
    def searchable(self) -> bool:
        return bool(self.isbn) or len(self.title_words) >= 2


Skip = Literal["other"]


def _heading_kind(line: str) -> Kind | Skip | None:
    """A short line naming a list ("Основная литература:", "Supplementary reading") switches the kind;
    "Other resources:" or "Интернет-источники" start a list that is not looked up."""
    if len(line) > 80 or _URL.search(line):
        return None
    stripped = line.strip().rstrip(":").strip()
    if len(_WORD.findall(stripped)) > 6:
        return None
    # "Core Concepts in Finance" is a title, "Core reading:" is a heading.
    if not (line.strip().endswith(":") or _LIST_WORD.search(stripped)):
        return None
    if _ONLINE_HEADING.search(stripped):
        return "other"
    if _SUPPLEMENTARY.search(stripped):
        return "supplementary"
    if _CORE.search(stripped):
        return "core"
    if _LITERATURE.search(stripped):
        return None  # "Литература:" on its own says nothing about core or not
    return "other" if line.strip().endswith(":") else None


_CONNECTORS = {"and", "и", "&", "et", "al", "және"}


def _full_name_authors(body: str) -> tuple[list[str], int] | None:
    """Authors written with full names, ended by ". ": the first author's names, and where the title starts.
    None unless the text before the first ". " is a list of two- or three-word capitalised names, so a title
    ("Cybersecurity Audit Essentials Tools, Techniques. Salihu A.") is not taken for authors."""
    stripped = body.strip()
    head, sep, _ = stripped.partition(". ")
    if not sep or len(head) > 90:
        return None
    groups = [
        [w for w in g.split() if w.lower() not in _CONNECTORS]
        for g in re.split(r",|\s(?:and|и|&|және)\s", head)
        if g.strip()
    ]
    if not groups or not all(2 <= len(g) <= 3 for g in groups):
        return None
    if not all(w[0].isupper() and _WORD.fullmatch(w) for g in groups for w in g):
        return None
    return groups[0], body.index(head) + len(head) + len(sep)


# A surname followed by initials, "Мэнкью Н.Г." / "Mankiw, N. G." / "Коттер Дж.", then any co-authors written
# the same way ("Гущина, О. М., Панюкова, Е. В."), which are not part of the title.
_INITIALS = r"(?:[^\W\d_]{1,2}\.\s*-?\s*){1,3}"
_FIRST_AUTHOR = re.compile(
    rf"\s*([^\W\d_][^\W\d_'’-]+)\s*,?\s+{_INITIALS}"
    rf"(?:\s*(?:,|и|and|&|және)?\s*[^\W\d_][^\W\d_'’-]+\s*,?\s+{_INITIALS})*[,.]?"
)
_STANDARD = re.compile(r"^\s*(?:ISO|IEC|ГОСТ|СТ\s+РК|IEEE\s+Std|NIST\s+SP)\b")


def _is_initial(word: str) -> bool:
    return len(word) <= 2 or (len(word) <= 3 and word.endswith("."))


def parse_reference(text: str, kind: Kind = "core") -> Reference:
    text = _NUMBERING.sub("", text).strip()
    isbn_match = _ISBN.search(text)
    isbn = re.sub(r"[^\dXx]", "", isbn_match.group(1)).upper() if isbn_match else None

    body = _YEAR_PARENS.sub(" ", _URL.sub(" ", text))
    words = _WORD.findall(body)
    surname: str | None = None
    name_words: list[str] = []
    rest_start = 0
    # The first author leads both GOST and APA. Without an author the reference starts with its title.
    first = _FIRST_AUTHOR.match(body)
    if first and first.group(1)[0].isupper():
        surname = first.group(1)
        rest_start = first.end()
    elif (names := _full_name_authors(body)) is not None:
        # "William Vaughan. Digital Modeling." / "Skoulikari Anna. Learning Git."
        name_words, rest_start = names
    title_part = body[rest_start:]
    end = _TITLE_END.search(title_part)
    if end and end.start() > 8:
        title_part = title_part[: end.start()]
    title_words = [
        w
        for w in _TITLE_WORD.findall(title_part)
        if len(w) >= 3 and w.lower() not in STOPWORDS and not _is_initial(w)
    ]
    if not title_words and words:
        title_words = [w for w in words if len(w) >= 3 and w.lower() not in STOPWORDS][:4]
    search_words = [
        part
        for w in _WORD.findall(title_part)
        if len(part := re.split(r"[-']", w)[0]) >= 3
        and part.lower() not in STOPWORDS
        and not _is_initial(part)
    ]
    return Reference(
        text=text,
        kind=kind,
        surname=surname,
        title_words=title_words[:6],
        isbn=isbn,
        name_words=name_words,
        search_words=search_words[:4] or title_words[:4],
    )


def _strip_template_label(line: str) -> str:
    while (m := _TEMPLATE_LABEL.match(line)) and m.end() < len(line):
        line = line[m.end() :]
    return line.strip()


def literature_lines(content: ExtractedContent) -> list[str]:
    """The literature section as the extractor found it; failing that, the Narxoz template's table row
    ("Required or recommended literature and other learning resources/tools | ..."), which the extractor
    flattens into "label | value" followed by the value's further lines."""
    if content.resources:
        return content.resources
    lines = content.raw_text.splitlines()
    for i, line in enumerate(lines):
        label, bar, value = line.partition(" | ")
        heading_only = not bar and len(line) < 80 and _LITERATURE.search(line) and _heading_kind(line)
        if not (bar and _LITERATURE.search(label)) and not heading_only:
            continue
        out = [value] if bar and value.strip() else [line] if heading_only else []
        for following in lines[i + 1 : i + 1 + _MAX_SECTION_LINES]:
            if " | " in following:  # the next row of the table
                break
            out.append(following)
        return out
    return []


def _entries(resources: list[str]) -> list[tuple[Kind | Skip, str]]:
    """The section's lines grouped into entries, each with the list it is under. In a numbered list a line
    without a number continues the entry before it (a reference wrapped over two lines)."""
    lines = [_strip_template_label(line.strip()) for raw in resources for line in raw.splitlines()]
    lines = [line for line in lines if line]
    numbered = sum(bool(re.match(r"^\s*(?:\[\d{1,3}\]|\d{1,3}[.)])", line)) for line in lines) >= 2
    kind: Kind | Skip = "core"
    entries: list[tuple[Kind | Skip, str]] = []
    joinable = False  # whether the next unnumbered line may continue the last entry
    for line in lines:
        if _SECTION_END.match(line):
            break
        heading = _heading_kind(line)
        if heading is not None:
            kind, joinable = heading, False
            continue
        starts_entry = bool(_NUMBERING.match(line))
        if entries and joinable and (_CONTINUATION.match(line) or (numbered and not starts_entry)):
            entries[-1] = (entries[-1][0], f"{entries[-1][1]} {line}")
            continue
        entries.append((kind, line))
        joinable = True
    return entries


def split_references(resources: list[str]) -> list[Reference]:
    """The literature section's references, minus headings, links, documentation, standards and prose."""
    out: list[Reference] = []
    for kind, line in _entries(resources):
        if kind == "other" or (_URL.search(line) and len(_URL.sub("", line).strip()) < 25):
            continue  # a website is not something the library holds
        if len(line) < 15 or len(_WORD.findall(line)) < 3:
            continue
        if _STANDARD.match(_NUMBERING.sub("", line)) or _NOT_A_BOOK.search(line):
            continue  # a standard (ISO, ГОСТ) or documentation, which a library does not lend
        ref = parse_reference(line, kind)
        if not ref.searchable:
            continue
        # A line of prose that ran into the section has no year, no ISBN, no author and no GOST separator.
        if not (_REFERENCE_MARK.search(line) or ref.surname or ref.name_words):
            continue
        out.append(ref)
    return out


def stems(words: list[str], length: int) -> list[str]:
    return [w.lower()[:length] for w in words]
