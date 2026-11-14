"""Findings in English, Russian and Kazakh: reviewed texts per criterion, and the AI's own text translated."""

from pathlib import Path

import pytest
import yaml

from app.ai.fake import FakeProvider
from app.ai.llm import LLMError
from app.ai.translate import translate_results
from app.ai.types import CriterionResultData, language_code
from app.db import pg
from app.enums import ResultStatus
from app.services.rubric import import_rubric, read_texts, set_texts, texts_path
from seed.samples import sample_syllabus
from tests.conftest import create_syllabus, upload

RUBRIC = Path(__file__).resolve().parents[1] / "rubric" / "2026.2.yaml"


def _result(cid: str, explanation: str, recommendation: str = "") -> CriterionResultData:
    return CriterionResultData(
        criterion_id=cid,
        status=ResultStatus.not_compliant,
        evidence="",
        explanation=explanation,
        recommendation=recommendation,
    )


def test_language_code():
    assert language_code("English") == "en"
    assert language_code("russian") == "ru"
    assert language_code("kk") == "kk"


@pytest.mark.parametrize("code", ["2026.2", "2026.3"])
def test_texts_cover_every_criterion_of_the_rubric(code):
    path = RUBRIC.with_name(f"{code}.yaml")
    rubric = yaml.safe_load(path.read_text(encoding="utf-8"))
    codes = {f"{p['number']}.{i}" for p in rubric["principles"] for i, _ in enumerate(p["criteria"], start=1)}
    texts = read_texts(texts_path(path))
    assert texts.rubric == rubric["code"]
    assert set(texts.criteria) == codes


def test_texts_need_every_language(tmp_path):
    path = tmp_path / "x.texts.yaml"
    path.write_text(
        'rubric: "x"\ncriteria:\n  "1.1":\n    problem: {en: a, ru: b}\n    fix: {en: a, ru: b, kk: c}\n',
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="no kk text"):
        read_texts(path)


async def test_translates_into_the_other_languages():
    results = [_result("1.1", "Goals are listed.", "Explain them."), _result("1.2", "")]
    out = await translate_results(FakeProvider(), results, source_language="English")
    first = out[0]
    assert first.language == "en"
    assert first.translations["en"].explanation == "Goals are listed."
    assert first.translations["ru"].explanation == "[ru] Goals are listed."
    assert first.translations["kk"].recommendation == "[kk] Explain them."
    # Nothing to translate: only the (empty) original.
    assert set(out[1].translations) == {"en"}


async def test_failed_translation_keeps_the_original():
    class Failing(FakeProvider):
        async def complete_json(self, **kwargs):
            raise LLMError("down")

    out = await translate_results(Failing(), [_result("1.1", "Goals are listed.")], source_language="English")
    assert out[0].explanation == "Goals are listed."
    assert set(out[0].translations) == {"en"}


async def test_results_carry_texts_and_translations(ref, client_for, tmp_path):
    async with pg.session_scope() as session:
        await import_rubric(session, RUBRIC)
    lead = client_for(ref["users"]["shalkar"])
    syllabus = await create_syllabus(lead, ref["courses"]["ai"].id)
    uploaded = await upload(lead, syllabus["id"], sample_syllabus("Artificial Intelligence", with_issues=True))

    run = (await lead.get(f"/versions/{uploaded['version']['id']}/runs/latest")).json()
    assert run["rubric_version"] == "2026.2"
    finding = next(r for r in run["results"] if r["severity"])
    assert set(finding["texts"]["problem"]) == {"en", "ru", "kk"}
    assert finding["language"] == "en"
    assert finding["translations"]["ru"]["explanation"].startswith("[ru] ")
    assert finding["translations"]["en"]["explanation"] == finding["explanation"]

    # Changing the texts needs no new rubric version.
    texts = read_texts(texts_path(RUBRIC))
    texts.criteria[finding["criterion_id"]].problem["en"] = "Rewritten."
    path = tmp_path / "2026.2.texts.yaml"
    path.write_text(yaml.safe_dump(texts.model_dump(), allow_unicode=True), encoding="utf-8")
    async with pg.session_scope() as session:
        assert await set_texts(session, path) == 49
    run = (await lead.get(f"/versions/{uploaded['version']['id']}/runs/latest")).json()
    again = next(r for r in run["results"] if r["criterion_id"] == finding["criterion_id"])
    assert again["texts"]["problem"]["en"] == "Rewritten."
