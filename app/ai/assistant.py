"""Section assistant of the Create Syllabus wizard."""

from app.ai.llm import LLMProvider
from app.config import get_settings

# The eight wizard steps of the prototype (CreateSyllabus) and the fields each one collects.
WIZARD_SECTIONS: dict[str, tuple[str, ...]] = {
    "Course information": ("title", "credits", "language", "semester", "degree_level"),
    "Academic context": ("program", "programRole", "purpose", "prerequisites"),
    "Learning outcomes": ("outcomes",),
    "Content": ("modules", "weeks"),
    "Teaching methods": ("methods", "participation"),
    "Assessment": ("assignments", "midterm", "project", "final"),
    "Resources": ("resources",),
    "Course policies": ("attendance", "integrity", "appeals"),
}

SYSTEM_PROMPT = """You assist a Narxoz University professor who is drafting a syllabus section by section.
Answer the question for the given section only. Be concrete and short (at most 150 words).
When asked to draft text, return text the professor can paste directly.
Learning outcomes must use observable verbs (compare, implement, evaluate, justify, design), never "understand".
Assessment weights must total 100%. Do not invent library titles; suggest checking the Narxoz library catalogue.
Answer in {language}."""


def _draft_summary(fields: dict[str, str]) -> str:
    filled = [f"{k}: {v}" for k, v in fields.items() if v and v.strip()]
    return "\n".join(filled) if filled else "(nothing filled in yet)"


async def answer(
    llm: LLMProvider, *, section: str, question: str, fields: dict[str, str], language: str
) -> str:
    return await llm.complete_text(
        purpose="assistant",
        system=SYSTEM_PROMPT.format(language=language or get_settings().llm_output_language),
        user=f"Section: {section}\nDraft so far:\n{_draft_summary(fields)}\n\nQuestion: {question}",
        meta={"section": section, "question": question, "fields": fields},
    )
