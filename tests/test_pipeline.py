"""AI pipeline without databases: extraction, deterministic rules, fake evaluator, evidence guard, apply."""

from pathlib import Path

from app.ai import pipeline
from app.ai.apply import Edit, apply_docx
from app.ai.evidence import verify
from app.ai.extraction import extract, parse_lines
from app.ai.fake import FakeProvider
from app.ai.types import CriterionResultData, CriterionSpec
from app.enums import CheckKind, EvaluationType, ResultStatus
from seed.samples import sample_syllabus

PROTOTYPE_ISSUES = {"1.2", "3.5", "4.3", "6.1", "6.4", "9.2"}


def _write(tmp_path: Path, data: bytes, name: str = "s.docx") -> Path:
    path = tmp_path / name
    path.write_bytes(data)
    return path


def _specs() -> list[CriterionSpec]:
    import yaml

    raw = yaml.safe_load(Path("rubric/2026.1.yaml").read_text())
    types = {
        "ai": EvaluationType.ai_semantic,
        "deterministic": EvaluationType.deterministic,
        "external": EvaluationType.external_context,
    }
    return [
        CriterionSpec(
            code=f"{p['number']}.{i}",
            name=c["name"],
            principle=p["number"],
            principle_name=p["name"],
            evaluation_type=types[c.get("evaluation_type", "ai")],
            rule=c.get("rule"),
            required_context=c.get("required_context"),
        )
        for p in raw["principles"]
        for i, c in enumerate(p["criteria"], start=1)
    ]


def test_extraction_reads_structure(tmp_path):
    content = extract(_write(tmp_path, sample_syllabus("Artificial Intelligence", with_issues=True)))
    assert content.language == "English"
    assert {"outcomes", "content", "assessment", "resources", "policy"} <= set(content.sections)
    assert [o.code for o in content.outcomes] == ["LO1", "LO2", "LO3", "LO4", "LO5"]
    assert len(content.weeks) == 12
    assert [a.weight for a in content.assessments if a.weight] == [30, 30, 40]


def test_breakdown_percentages_are_not_task_weights(tmp_path):
    content = extract(_write(tmp_path, sample_syllabus("AI", with_issues=False)))
    assert sum(a.weight or 0 for a in content.assessments) == 100


def test_language_detection():
    assert parse_lines([("Оқыту нәтижелері", True), ("ОН1. Талдау жасау", False)]).language == "Kazakh"
    assert (
        parse_lines([("Результаты обучения", True), ("РО1. Сравнивать методы", False)]).language == "Russian"
    )


async def test_sample_with_issues_reproduces_prototype_review(tmp_path):
    content = extract(_write(tmp_path, sample_syllabus("Artificial Intelligence", with_issues=True)))
    output = await pipeline.evaluate(content, _specs(), FakeProvider(), trace=False)
    assert len(output.results) == 49
    by_code = {r.criterion_id: r for r in output.results}
    issues = {c for c, r in by_code.items() if r.status in (ResultStatus.partial, ResultStatus.not_compliant)}
    assert issues == PROTOTYPE_ISSUES
    assert by_code["6.1"].status == ResultStatus.not_compliant
    for code in ("3.4", "7.2"):
        assert by_code[code].status == ResultStatus.cannot_verify
        assert by_code[code].required_context
    assert all(r.evidence_verified for r in output.results if r.status != ResultStatus.cannot_verify)


async def test_clean_sample_has_no_issues(tmp_path):
    content = extract(_write(tmp_path, sample_syllabus("AI", with_issues=False)))
    output = await pipeline.evaluate(content, _specs(), FakeProvider(), trace=False)
    assert pipeline.count_issues(output.results) == 0
    assert output.technical.issues == []


async def test_passport_enables_program_alignment(tmp_path):
    content = extract(_write(tmp_path, sample_syllabus("AI", with_issues=False)))
    output = await pipeline.evaluate(
        content, _specs(), FakeProvider(), trace=False, context={"Educational Program Passport": "PO1 ..."}
    )
    assert next(r for r in output.results if r.criterion_id == "3.4").status == ResultStatus.compliant


async def test_unchecked_criteria_are_skipped(tmp_path):
    content = extract(_write(tmp_path, sample_syllabus("AI", with_issues=True)))
    output = await pipeline.evaluate(
        content, _specs(), FakeProvider(), trace=False, checks={CheckKind.technical}
    )
    assert {r.criterion_id for r in output.results} == {"3.2", "3.4", "6.8", "7.2", "8.2"}


def test_invented_evidence_is_downgraded():
    result = CriterionResultData(
        criterion_id="3.5",
        status=ResultStatus.compliant,
        evidence="Students will design a compiler.",
        explanation="ok",
        recommendation="",
        confidence=0.9,
    )
    checked = verify(result, "LO1. Explain methods.\nLO2. Compare methods.", threshold=85)
    assert checked.status == ResultStatus.cannot_verify
    assert checked.evidence_verified is False
    assert checked.confidence is None


def test_apply_edits_paragraphs_and_table_rows(tmp_path):
    path = _write(tmp_path, sample_syllabus("AI", with_issues=True))
    result = apply_docx(
        path,
        [
            Edit(
                "LO2. Students will understand the fundamental principles of machine learning.",
                "LO2. Compare machine-learning methods and justify their selection.",
            ),
            Edit("Final project | 40% | LO3, LO4", "Final project | 40% | LO3, LO4, LO5"),
            Edit("Text that is not in the document", "New paragraph"),
        ],
    )
    assert (result.applied, result.appended) == (2, 1)
    content = extract(_write(tmp_path, result.data, "v2.docx"))
    assert "LO2. Compare machine-learning methods and justify their selection." in content.paragraphs
    assert content.assessments[-1].outcomes == ["LO3", "LO4", "LO5"]
    assert content.paragraphs[-1] == "New paragraph"


class _FakeDecider:
    """Passes every criterion except the given ones, which it marks Not compliant; skips `skip`."""

    model_version = "fake-decider"

    def __init__(self, failing: set[str], skip: frozenset[str] = frozenset()) -> None:
        self.failing, self.skip = failing, skip
        self.asked: list[str] = []

    async def decide(self, state, specs, *, run_id=None, trace=True):  # type: ignore[no-untyped-def]
        from app.ai.decisions import Decision

        self.asked = [s.code for s in specs]
        assert state and state[0].startswith("Syllabus:")
        return {
            s.code: Decision(ResultStatus.not_compliant if s.code in self.failing else ResultStatus.compliant, 0.9)
            for s in specs
            if s.code not in self.skip
        }


class _CountingProvider(FakeProvider):
    def __init__(self) -> None:
        self.calls: list[tuple[str, list[str]]] = []

    async def complete_json(self, *, purpose, system, user, schema, meta=None, run_id=None, trace=True):  # type: ignore[no-untyped-def]
        self.calls.append((purpose, [s.code for s in (meta or {}).get("criteria", [])]))
        return await super().complete_json(
            purpose=purpose, system=system, user=user, schema=schema, meta=meta, run_id=run_id, trace=trace
        )


async def test_decision_model_decides_and_chat_model_only_explains_failures(tmp_path):
    content = extract(_write(tmp_path, sample_syllabus("Artificial Intelligence", with_issues=True)))
    decider = _FakeDecider(failing={"1.2", "6.1"})
    llm = _CountingProvider()
    output = await pipeline.evaluate(content, _specs(), llm, trace=False, decider=decider)

    assert len(output.results) == 49
    by_code = {r.criterion_id: r for r in output.results}
    # Only the two failing criteria went to the chat model, each with its principle, and only to explain.
    assert sorted(llm.calls) == [("explain", ["1.2"]), ("explain", ["6.1"])]
    assert by_code["6.1"].status == ResultStatus.not_compliant
    assert by_code["6.1"].evidence and by_code["6.1"].recommendation
    assert by_code["6.1"].confidence == 0.9
    passed = by_code["2.1"]
    assert passed.status == ResultStatus.compliant and passed.evidence == "" and "decision model" in passed.explanation
    # Rules and missing external context are still decided without any model.
    assert "3.4" not in decider.asked and by_code["3.4"].status == ResultStatus.cannot_verify
    assert output.model_version == "fake-decider+fake-evaluator"


async def test_criteria_the_decision_model_skips_are_decided_by_the_chat_model(tmp_path):
    content = extract(_write(tmp_path, sample_syllabus("Artificial Intelligence", with_issues=True)))
    llm = _CountingProvider()
    output = await pipeline.evaluate(
        content, _specs(), llm, trace=False, decider=_FakeDecider(failing=set(), skip=frozenset({"6.1"}))
    )
    assert llm.calls == [("criteria", ["6.1"])]
    assert next(r for r in output.results if r.criterion_id == "6.1").status == ResultStatus.not_compliant


def test_decision_questions_and_answers_round_trip():
    from app.ai.decisions import build_questions, parse_answers

    specs = [s for s in _specs() if s.code in ("1.2", "3.5")]
    questions = build_questions(specs)
    assert set(questions) == {"c_1_2", "c_3_5"}
    assert questions["c_1_2"]["type"] == "choice"
    assert list(questions["c_1_2"]["criteria"]) == [s.value for s in ResultStatus]
    parsed = parse_answers(specs, {"c_1_2": {"choice": "Partially compliant", "confidence": 0.7}, "c_3_5": {"choice": "?"}})
    assert set(parsed) == {"1.2"} and parsed["1.2"].status == ResultStatus.partial


def test_severity_of_findings():
    from app.enums import Severity, severity

    assert severity(ResultStatus.compliant, True) is None
    assert severity(ResultStatus.cannot_verify, True) is None
    assert severity(ResultStatus.partial, True) == Severity.critical
    assert severity(ResultStatus.not_compliant, False) == Severity.major
    assert severity(ResultStatus.partial, False) == Severity.minor

    def result(code: str, status: ResultStatus) -> CriterionResultData:
        return CriterionResultData(
            criterion_id=code, status=status, evidence="", explanation="", recommendation=""
        )

    results = [
        result("7.1", ResultStatus.partial),
        result("7.2", ResultStatus.not_compliant),
        result("7.3", ResultStatus.partial),
        result("7.4", ResultStatus.compliant),
    ]
    counts = pipeline.count_by_severity(results, {"7.1", "7.4"})
    assert counts == {Severity.critical: 1, Severity.major: 1, Severity.minor: 1}
    assert sum(counts.values()) == pipeline.count_issues(results)


def test_rubric_critical_flags_parse():
    import yaml

    from app.services.rubric import RubricFile

    path = Path(__file__).resolve().parents[1] / "rubric" / "2026.2.yaml"
    data = RubricFile.model_validate(yaml.safe_load(path.read_text(encoding="utf-8")))
    flagged = [c.name for p in data.principles for c in p.criteria if c.critical]
    assert "Every learning outcome is assessed" in flagged
    assert len(flagged) == 7
