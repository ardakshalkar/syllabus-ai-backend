"""Which program learning outcomes each course outcome contributes to.

One request per syllabus: the course's outcomes and the candidate programs' outcomes go in, links come back.
Every link names a course outcome, a program and an outcome number that were actually given; anything else
the model returns is dropped. The links are information for the School and the Provost Office (which
program outcomes the courses cover); grading criterion 3.4 stays with the rubric pipeline.
"""

import json
import logging
import re
import uuid
from dataclasses import dataclass

from app.ai.llm import LLMProvider
from app.ai.types import ExtractedContent
from app.models import LinkOrigin

log = logging.getLogger(__name__)

MAX_LINKS_PER_OUTCOME = 3

SYSTEM_PROMPT = """You map the learning outcomes of one university course to the learning outcomes of the
educational programs it may serve, at Narxoz University (Kazakhstan).

Link a course outcome to a program outcome only when achieving the course outcome clearly develops that
program outcome: the same competence, applied in the same field or to the same kind of task. Do not link on a
shared generic verb or word alone ("analyses", "uses information technology", "works in a team") unless the
course outcome is really about that. A course outcome may support no outcome of a program, or up to
{most} of them. The programs are candidates: the course may not belong to all of them, so judge each link on
its own.

Answer in JSON. For each link give the course outcome's code as given, the program's code, the program
outcome's number, and one short sentence saying why, in {language}."""

COURSE_TEXT_NOTE = (
    "The course outcomes were not separated out; find them in this text and use their own numbering, or 1, 2, 3"
    " in order if they have none."
)


@dataclass
class ProgramOutcomes:
    code: str
    outcomes: dict[int, str]  # number -> text, in the language chosen for the request


@dataclass
class Link:
    course_outcome: str
    course_outcome_text: str
    program: str
    outcome: int
    reason: str
    origin: LinkOrigin = LinkOrigin.inferred  # every link `align` finds is the model's own reading


def course_outcomes(content: ExtractedContent) -> tuple[list[dict[str, str]], str]:
    """The course outcomes as items, or, when extraction found none, the text of the outcomes section."""
    if content.outcomes:
        return [{"code": o.code, "text": o.text} for o in content.outcomes], ""
    return [], (content.sections.get("outcomes") or "").strip()


_SCHEMA = {
    "type": "object",
    "properties": {
        "links": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "course_outcome": {"type": "string"},
                    "course_outcome_text": {"type": "string"},
                    "program": {"type": "string"},
                    "outcome": {"type": "integer"},
                    "reason": {"type": "string"},
                },
                "required": ["course_outcome", "course_outcome_text", "program", "outcome", "reason"],
            },
        }
    },
    "required": ["links"],
}


def _key(code: str) -> str:
    return re.sub(r"\W+", "", code).casefold()


async def align(
    llm: LLMProvider,
    content: ExtractedContent,
    programs: list[ProgramOutcomes],
    *,
    language: str = "English",
    run_id: uuid.UUID | None = None,
) -> list[Link]:
    items, text = course_outcomes(content)
    if not programs or not (items or text):
        return []
    user: dict = {
        "programs": [
            {"code": p.code, "outcomes": [{"number": n, "text": t} for n, t in sorted(p.outcomes.items())]}
            for p in programs
        ]
    }
    if items:
        user["course_outcomes"] = items
    else:
        user["course_outcomes_text"] = text[:6000]
        user["note"] = COURSE_TEXT_NOTE
    answer = await llm.complete_json(
        purpose="outcome_alignment",
        system=SYSTEM_PROMPT.format(most=MAX_LINKS_PER_OUTCOME, language=language),
        user=json.dumps(user, ensure_ascii=False),
        schema=_SCHEMA,
        meta={"course_outcomes": items, "course_text": text, "programs": programs},
        run_id=run_id,
    )

    given = {_key(i["code"]): i for i in items}
    by_code = {p.code: p for p in programs}
    links: list[Link] = []
    seen: set[tuple[str, str, int]] = set()
    per_outcome: dict[tuple[str, str], int] = {}
    for raw in answer.get("links") or []:
        try:
            code, program, number = (
                str(raw["course_outcome"]).strip(),
                str(raw["program"]).strip(),
                int(raw["outcome"]),
            )
        except (KeyError, TypeError, ValueError):
            continue
        p = by_code.get(program.upper())
        if p is None or number not in p.outcomes or not code:
            continue
        if items:
            item = given.get(_key(code))
            if item is None:
                continue  # an outcome the syllabus does not have
            code, outcome_text = item["code"], item["text"]
        else:
            outcome_text = str(raw.get("course_outcome_text") or "").strip()
            if not outcome_text:
                continue
        if (code, p.code, number) in seen or per_outcome.get((code, p.code), 0) >= MAX_LINKS_PER_OUTCOME:
            continue
        seen.add((code, p.code, number))
        per_outcome[(code, p.code)] = per_outcome.get((code, p.code), 0) + 1
        links.append(Link(code[:40], outcome_text, p.code, number, str(raw.get("reason") or "").strip()))
    return links
