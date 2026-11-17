"""The syllabus as numbered blocks, each with the kind of content it holds.

A block is one line of `ExtractedContent.raw_text`: a paragraph, a list item or a table row (the Markdown keeps a
table row on one line). The explanation step reads the syllabus as `[12·policy] text` and cites a block by its
number, so the quote is taken from a known line instead of being copied by the model, and a finding can point
at its place in the document.

Categories come from the template's row labels where the line has one ("Attendance policy | ..." in English,
Russian or Kazakh); the model sorts the rest once per syllabus version, and the result is kept with the
extracted content.
"""

import logging
import re
import uuid
from typing import Any

from app.ai.llm import LLMProvider
from app.ai.types import Block, ExtractedContent
from app.config import get_settings

log = logging.getLogger(__name__)

CATEGORIES = (
    "course_info",  # title, code, credits, lecturer, semester, language, format
    "goals",  # course goal and aim
    "program",  # place in the educational program, prerequisites, program outcomes
    "outcomes",  # learning outcomes of the course
    "content",  # topics, modules, weekly plan
    "methods",  # teaching methods and learning activities
    "assessment",  # assessment tasks, their criteria and weights
    "grading",  # final grade, grading scale, admission to the exam
    "resources",  # literature and other learning resources
    "policy",  # attendance, academic integrity, conduct, appeals, inclusion
    "other",
)

# (category, fragments of a row label in English, Russian or Kazakh), tried in order.
_LABELS: list[tuple[str, tuple[str, ...]]] = [
    ("program", ("prerequisite", "пререквизит")),
    ("outcomes", ("learning outcome", "результаты обучения", "оқыту нәтижелері")),
    ("goals", ("course goal", "aim of the course", "цель дисциплины", "цель курса", "пәннің мақсаты")),
    (
        "methods",
        (
            "teaching method",
            "learning activit",
            "методы преподавания",
            "учебная деятельность",
            "оқыту әдістері",
        ),
    ),
    (
        "grading",
        (
            "overall course grade",
            "final grade",
            "итоговая оценка",
            "қорытынды баға",
            "final exam",
            "допуск",
            "жіберу",
            "grading scale",
            "шкала",
        ),
    ),
    ("assessment", ("assessment", "оценивани", "бағалау")),
    ("resources", ("literature", "learning resources", "литература", "ресурс", "әдебиет")),
    (
        "policy",
        (
            "attendance",
            "посещаем",
            "қатысу",
            "academic behavior",
            "академического поведения",
            "академиялық тәртіп",
            "diversity",
            "многообраз",
            "әртүрлілік",
        ),
    ),
    ("content", ("course content", "содержание дисциплины", "пәннің мазмұны")),
    (
        "course_info",
        (
            "course title",
            "course code",
            "ects",
            "academic year",
            "semester",
            "language of instruction",
            "mode of delivery",
            "lecturer",
            "type of course",
            "level of course",
            "approved",
            "название дисциплины",
            "код дисциплины",
            "кредит",
            "учебный год",
            "семестр",
            "язык обучения",
            "форма обучения",
            "преподавател",
            "тип дисциплины",
            "уровень дисциплины",
            "одобрен",
            "пәннің атауы",
            "пәннің коды",
            "оқу жылы",
            "оқыту тілі",
            "оқыту түрі",
            "оқытушы",
            "пәннің түрі",
            "пәннің деңгейі",
        ),
    ),
]
_RULE = re.compile(r"^\s*\|?[\s:|-]+\|?\s*$")  # a Markdown table's header rule: | --- | --- |
_GRADE = re.compile(r"^(a|b|c|d|f|fx)[+-]?$")

SYSTEM = """You sort the lines of a university course syllabus by what they are about.
Each line starts with its number in brackets. For every number you are asked about, give one category:
- course_info: course title, code, credits, lecturer, semester, language, format of study
- goals: the course goal or aim
- program: the course's place in the educational program, prerequisites, program outcomes
- outcomes: the learning outcomes of the course
- content: topics, modules, the weekly plan
- methods: teaching methods and learning activities
- assessment: assessment tasks, their criteria and weights
- grading: the final grade, grading scale, admission to the final exam
- resources: literature and other learning resources
- policy: attendance, academic integrity, conduct, appeals, inclusion
- other: anything else (signatures, approval notes, empty headings)
A heading takes the category of the section it opens. Lines already sorted are shown with their category."""


def lines(raw_text: str) -> list[str]:
    """The lines of the text that become blocks: every non-empty line except a table's header rule."""
    return [line.strip() for line in raw_text.splitlines() if line.strip() and not _RULE.match(line)]


# A block longer than this is split, so a quote points at a passage rather than at a whole table: Markdown keeps a
# table row on one line, cell breaks become double spaces, and a nested table can make one row a page long.
MAX_BLOCK = 300
_BREAK = re.compile(r"\s{2,}| \| ")
_DASHES = re.compile(r"^[\s|:-]*$")


_SENTENCE = re.compile(r"(?<=[.;!?])\s+")


def _short(part: str) -> list[str]:
    """A passage with no breaks, cut at sentence ends and, failing that, between words, to MAX_BLOCK."""
    if len(part) <= MAX_BLOCK:
        return [part]
    out = []
    for sentence in _SENTENCE.split(part):
        while len(sentence) > MAX_BLOCK:
            cut = sentence.rfind(" ", 0, MAX_BLOCK)
            cut = cut if cut > 0 else MAX_BLOCK
            out.append(sentence[:cut])
            sentence = sentence[cut:].strip()
        out.append(sentence)
    return out


def pieces(raw_text: str) -> list[tuple[str, str | None]]:
    """(text, category from the row's label or None) for every block: each line, a long one split at its cell
    and line breaks into passages of up to about MAX_BLOCK characters. The passages of a row share its label."""
    out: list[tuple[str, str | None]] = []
    for line in lines(raw_text):
        category, text = by_label(line), text_of(line)
        if len(text) <= MAX_BLOCK:
            out.append((text, category))
            continue
        current = ""
        for part in (p.strip() for p in _BREAK.split(text) for p in _short(p)):
            if not part or _DASHES.match(part):
                continue
            if current and len(current) + 1 + len(part) > MAX_BLOCK:
                out.append((current, category))
                current = ""
            current = f"{current} {part}".strip()
        if current:
            out.append((current, category))
    return out


def text_of(line: str) -> str:
    """A block as it is quoted: a table row without its pipes and Markdown emphasis."""
    cells = [c.strip() for c in line.strip().strip("|").split("|")] if "|" in line else [line]
    return " | ".join(c for c in cells if c).replace("**", "").strip()


def by_label(line: str) -> str | None:
    """The category a template row's label gives, e.g. "| Attendance policy | ... |" -> policy."""
    if "|" not in line:
        return None
    label = re.sub(r"\s+", " ", text_of(line).split(" | ")[0]).strip().lower()
    if _GRADE.match(label):
        return "grading"
    if re.fullmatch(r"\d{1,2}\.?", label):
        return "content"  # a row of the weekly plan
    for category, fragments in _LABELS:
        if any(f in label for f in fragments):
            return category
    return None


def _schema(ids: list[int]) -> dict[str, Any]:
    return {
        "type": "object",
        "additionalProperties": False,
        "required": ["lines"],
        "properties": {
            "lines": {
                "type": "array",
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["n", "category"],
                    "properties": {
                        "n": {"type": "integer", "enum": ids},
                        "category": {"type": "string", "enum": list(CATEGORIES)},
                    },
                },
            }
        },
    }


async def categorize(
    raw_text: str, llm: LLMProvider, *, run_id: uuid.UUID | None = None, trace: bool = True
) -> list[Block]:
    """Every line of the text as a block with a category: by its label where it has one, otherwise as the
    model sorts it. A line the model leaves out, or every line when the model fails, takes the category of the
    line before it."""
    texts = pieces(raw_text)
    known = {n: category for n, (_, category) in enumerate(texts, start=1) if category}
    ask = [n for n in range(1, len(texts) + 1) if n not in known]
    if ask:
        shown = "\n".join(
            f"[{n}{'·' + known[n] if n in known else ''}] {text}"
            for n, (text, _) in enumerate(texts, start=1)
        )
        try:
            data = await llm.complete_json(
                purpose="blocks",
                system=SYSTEM,
                user=f"{shown}\n\nSort lines: {', '.join(map(str, ask))}",
                schema=_schema(ask),
                run_id=run_id,
                trace=trace,
            )
            for item in data.get("lines", []):
                if item.get("n") in ask and item.get("category") in CATEGORIES:
                    known.setdefault(item["n"], item["category"])
        except Exception:  # categories help; a check must not fail without them
            log.exception("could not sort the syllabus lines; they keep the category of the line before")
    blocks, previous = [], "course_info"
    for n, (text, _) in enumerate(texts, start=1):
        previous = known.get(n, previous)
        blocks.append(Block(text=text, category=previous))
    return blocks


async def ensure(content: ExtractedContent, llm: LLMProvider) -> bool:
    """Give the content its blocks when EXPLAIN_BLOCKS is on and it has none yet; True when it got them."""
    if content.blocks or not get_settings().explain_blocks or get_settings().llm_provider == "fake":
        return False
    content.blocks = await categorize(content.raw_text, llm)
    return bool(content.blocks)


def render(blocks: list[Block], limit: int) -> str:
    """The blocks as the model reads them, `[n·category] text`, cut at about `limit` characters."""
    out, size = [], 0
    for n, block in enumerate(blocks, start=1):
        line = f"[{n}·{block.category}] {block.text}"
        if size + len(line) > limit:
            out.append("[document truncated]")
            break
        out.append(line)
        size += len(line) + 1
    return "\n".join(out)
