"""Translating a check's explanations and recommendations into the app's other languages.

The criteria prompts write in one language (LLM_OUTPUT_LANGUAGE) so the evaluation stays focused; this step
runs afterwards and puts the same texts in the other languages, so a professor reads them in the language
the app is set to. A failed translation never fails a check: those results keep their original text only.
"""

import asyncio
import json
import logging
import uuid
from typing import Any

from app.ai.llm import LLMError, LLMProvider
from app.ai.types import LANGUAGE_NAMES, LANGUAGES, CriterionResultData, ResultText, language_code

log = logging.getLogger(__name__)

# Items per request: the answer stays short enough for every model's output limit.
CHUNK = 12

SYSTEM_PROMPT = """You translate the findings of a university syllabus review at Narxoz University (Kazakhstan).
Each item has an "explanation" (why the syllabus meets a criterion or not) and a "recommendation" (what the
professor should change), written in {source}. Translate both into {targets}.

Rules:
- Keep the meaning exactly; do not add, drop or soften anything.
- Keep quotations from the syllabus, course codes, learning outcome codes (LO1, РО2, ОН3), numbers, percentages
  and book titles as they are.
- Use the standard academic terms of Kazakhstani higher education:
  learning outcomes = результаты обучения = оқу нәтижелері; syllabus = силлабус = силлабус;
  assessment = оценивание = бағалау; educational program = образовательная программа = білім беру бағдарламасы;
  prerequisites = пререквизиты = пререквизиттер.
- Write natural, formal language addressed to a professor. An empty text stays empty.
"""


def _schema(targets: list[str]) -> dict[str, Any]:
    text = {
        "type": "object",
        "additionalProperties": False,
        "required": ["explanation", "recommendation"],
        "properties": {"explanation": {"type": "string"}, "recommendation": {"type": "string"}},
    }
    return {
        "type": "object",
        "additionalProperties": False,
        "required": ["items"],
        "properties": {
            "items": {
                "type": "array",
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["criterion_id", *targets],
                    "properties": {"criterion_id": {"type": "string"}, **{lang: text for lang in targets}},
                },
            }
        },
    }


async def _translate_chunk(
    llm: LLMProvider,
    chunk: list[CriterionResultData],
    source: str,
    targets: list[str],
    run_id: uuid.UUID | None,
) -> dict[str, dict[str, ResultText]]:
    items = [
        {
            "criterion_id": r.criterion_id,
            "text": {"explanation": r.explanation, "recommendation": r.recommendation},
        }
        for r in chunk
    ]
    answer = await llm.complete_json(
        purpose="translate",
        system=SYSTEM_PROMPT.format(
            source=LANGUAGE_NAMES.get(source, source),
            targets=", ".join(LANGUAGE_NAMES[t] for t in targets),
        ),
        user=json.dumps({"items": items}, ensure_ascii=False),
        schema=_schema(targets),
        meta={"items": items, "targets": targets},
        run_id=run_id,
    )
    wanted = {r.criterion_id for r in chunk}
    out: dict[str, dict[str, ResultText]] = {}
    for item in answer.get("items", []):
        cid = item.get("criterion_id")
        if cid not in wanted:
            continue
        out[cid] = {
            lang: ResultText.model_validate(item[lang])
            for lang in targets
            if isinstance(item.get(lang), dict)
        }
    return out


async def translate_results(
    llm: LLMProvider,
    results: list[CriterionResultData],
    *,
    source_language: str,
    run_id: uuid.UUID | None = None,
) -> list[CriterionResultData]:
    """The results with `language` set and `translations` filled for every language of the app, the original
    language included. Results whose translation failed keep only the original."""
    source = language_code(source_language)
    targets = [lang for lang in LANGUAGES if lang != source]
    texts = [r for r in results if r.explanation.strip() or r.recommendation.strip()]
    chunks = [texts[i : i + CHUNK] for i in range(0, len(texts), CHUNK)]
    answers = await asyncio.gather(
        *(_translate_chunk(llm, c, source, targets, run_id) for c in chunks), return_exceptions=True
    )
    translated: dict[str, dict[str, ResultText]] = {}
    for answer in answers:
        if isinstance(answer, LLMError | ValueError | KeyError):
            log.warning("could not translate part of the findings of run %s: %s", run_id, answer)
        elif isinstance(answer, BaseException):
            raise answer
        else:
            translated.update(answer)
    if texts and not translated:
        log.warning("no findings of run %s were translated", run_id)
    out = []
    for r in results:
        own = ResultText(explanation=r.explanation, recommendation=r.recommendation)
        found = translated.get(r.criterion_id, {})
        out.append(r.model_copy(update={"language": source, "translations": {**found, source: own}}))
    return out
