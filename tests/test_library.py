"""Library (MegaPro) lookup without the network: references, the OPAC results page, matching, the status rule."""

import httpx
import pytest

from app.ai.types import CriterionSpec, ExtractedContent
from app.enums import EvaluationType, ResultStatus
from app.library import lookup, megapro
from app.library.config import LibraryConfig, parse
from app.library.references import parse_reference, split_references

# One record as the OPAC renders it (trimmed from a real results page).
_RECORD = """
<div id="{id}" class="rs-doc"> <div class="rs-ctr"> <b>1. Документ</b> <div class="bc-wrap">
<img class="zoom" src="/MegaPro/DownLoad/GetBookCover/{id}" alt="bookCover"> </div> </div>
<div class="rs-data"> <div class="rs-item "> {description}</div>
<div class="rs-item "><b>Авторы: </b>{authors}</div>
<div class="rs-item "><b>Шифры: </b>HB171.5 - .M36 2013</div>
{copies}
<div class="rs-item"><a target="_blank" href="../MarcFormat/{id}">Формат RUSMARC</a></div>
<div class="rs-item "><b>Дисциплина КО: </b>Экономика</div>
<div class="rs-item"><a href="#" data-docid="{id}" class="copyToClipboard">Копирование биб.описания в буфер обмена</a></div>
<div class="rs-item rs-roll rs-roll-out">Подробнее</div>
<div class="detail"> <div class="rs-item "><b>Аннотация: </b>Учебник по экономике.</div> </div> </div> </div>
"""


def _page(*records: str, total: int | None = None) -> str:
    n = total if total is not None else len(records)
    return f"<html><body><td>Найдено документов - {n}</td>{''.join(records)}</body></html>"


def _record(id: str, description: str, authors: str = "", copies: int | None = 2) -> str:
    c = (
        f'<div class="rs-item "><b>Экземпляры: </b>Всего: {copies}, из них: хр-1, чз №3-1</div>'
        if copies
        else ""
    )
    return _RECORD.format(id=id, description=description, authors=authors, copies=c)


MANKIW = _record(
    "101",
    "Мэнкью Н.Г. Принципы экономики [Текст] : учебник / Н.Г. Мэнкью. - 5-е изд. - СПб. : Питер, 2013. - 672 с.",
    "Мэнкью Н.Г.",
)


def test_parse_reference_gost_and_apa():
    gost = parse_reference("1. Мэнкью Н.Г. Принципы экономики. – 5-е изд. – СПб.: Питер, 2013. – 672 с.")
    assert gost.surname == "Мэнкью"
    assert gost.title_words[:2] == ["Принципы", "экономики"]

    apa = parse_reference("Mankiw, N. G. (2021). Principles of economics (9th ed.). Cengage Learning.")
    assert apa.surname == "Mankiw"
    assert apa.title_words[:2] == ["Principles", "economics"]

    isbn = parse_reference("Kotler P. Marketing Management. Pearson, 2016. ISBN 978-0-13-385646-0")
    assert isbn.isbn == "9780133856460"


def test_split_references_core_supplementary_and_links():
    refs = split_references(
        [
            "Основная литература:",
            "1. Мэнкью Н.Г. Принципы экономики. – СПб.: Питер, 2013.",
            "2. Kotler P., Keller K. Marketing Management. – Pearson, 2016.",
            "Дополнительная литература:",
            "3. Портер М. Конкурентная стратегия. – М.: Альпина, 2011.",
            "Интернет-ресурсы:",
            "https://www.investopedia.com",
            "4. Some Article Title that is really a website description",
        ]
    )
    assert [(r.surname, r.kind) for r in refs] == [
        ("Мэнкью", "core"),
        ("Kotler", "core"),
        ("Портер", "supplementary"),
    ]


def test_a_book_titled_like_a_heading_is_still_a_reference():
    refs = split_references(
        ["Core Concepts in Corporate Finance and Banking Practice, 2019", "Brealey R. Corporate Finance."]
    )
    assert len(refs) == 2 and all(r.kind == "core" for r in refs)


def test_parse_results_page():
    total, records = megapro.parse_results(
        _page(MANKIW, _record("102", "Без экземпляров [Текст]", copies=None))
    )
    assert total == 2
    first = records[0]
    assert first.id == "101" and first.copies == 2 and first.authors == "Мэнкью Н.Г."
    assert first.description.startswith("Мэнкью Н.Г. Принципы экономики")
    assert "Аннотация" not in first.description and "Подробнее" not in first.description
    assert first.disciplines == ["Экономика"]
    assert records[1].copies is None and not records[1].electronic


def test_yaml_placeholders_come_from_the_environment(monkeypatch):
    monkeypatch.setenv("MEGAPRO_DB_ID", "42")
    cfg = parse("library:\n  provider: megapro_api\n  megapro_api:\n    db_id: ${MEGAPRO_DB_ID}\n")
    assert cfg.provider == "megapro_api" and cfg.megapro_api.db_id == "42"


def test_shipped_yaml_is_valid():
    from pathlib import Path

    cfg = parse(Path("integrations/library.yaml").read_text(encoding="utf-8"))
    assert cfg.provider == "megapro_opac" and cfg.megapro_opac.databases == [0, 1]
    assert cfg.extra_ca_file and cfg.extra_ca_file.exists()


def _config() -> LibraryConfig:
    cfg = LibraryConfig(provider="megapro_opac", base_url="https://megapro.test/MegaPro")
    cfg.limits.requests_per_second = 1000
    return cfg


def _fake_opac(monkeypatch, handler) -> list[str]:
    """Route OPAC requests to `handler(terms, database)`; return the searched terms."""
    searched: list[str] = []
    state = {"db": 0}

    def respond(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        form = dict(httpx.QueryParams(request.content.decode())) if request.content else {}
        if path.endswith("/Home/ChangeDb"):
            state["db"] = int(form["DbList"])
            return httpx.Response(200, text="ok")
        if path.endswith("/SearchResult/Simple"):
            searched.append(form["simpleCond"])
            page = handler(form["simpleCond"], state["db"])
            if page is None:  # nothing found: MegaPro stays on the form
                return httpx.Response(200, text="<form></form>")
            return httpx.Response(200, text=page)
        return httpx.Response(200, text="<form></form>")

    transport = httpx.MockTransport(respond)
    monkeypatch.setattr(megapro, "client", lambda config: megapro.OpacClient(config, transport=transport))

    async def no_cache(*_a, **_k):
        return None

    monkeypatch.setattr(lookup.cache, "get_json", no_cache)
    monkeypatch.setattr(lookup.cache, "put_json", no_cache)
    return searched


SPEC = CriterionSpec(
    code="7.3",
    name="Core literature is held in the university's Scientific Library",
    principle=7,
    principle_name="Resources",
    evaluation_type=EvaluationType.external_context,
    required_context=lookup.CONTEXT,
)


def _content(*lines: str) -> ExtractedContent:
    return ExtractedContent(raw_text="\n".join(lines), paragraphs=list(lines), resources=list(lines))


@pytest.mark.asyncio
async def test_lookup_finds_held_title_and_misses_the_other(monkeypatch):
    def handler(terms: str, db: int) -> str | None:
        return _page(MANKIW) if "мэнкью" in terms and db == 0 else None

    searched = _fake_opac(monkeypatch, handler)
    report = await lookup.check_literature(
        _content(
            "Основная литература:",
            "1. Мэнкью Н.Г. Принципы экономики. – СПб.: Питер, 2013.",
            "2. Неизвестный А.Б. Несуществующая книга о финансах. – М., 2020.",
        ),
        _config(),
    )
    assert report is not None
    assert [r.status for r in report.references] == ["held", "not_found"]
    assert searched[0] == "мэнкью принци эконом"  # surname + stems, matched as "starts with"
    result = lookup.criterion_result(SPEC, report, _config())
    assert result.status == ResultStatus.partial  # 1 of 2 = 0.5
    assert "1 of 2 core titles" in result.explanation
    assert "Несуществующая" in result.recommendation


@pytest.mark.asyncio
async def test_a_record_that_only_shares_words_is_not_a_match(monkeypatch):
    other = _record(
        "103", "Мэнкью Н.Г. Макроэкономика [Текст] : учебник. - СПб. : Питер, 2010.", "Мэнкью Н.Г."
    )
    _fake_opac(monkeypatch, lambda terms, db: _page(other))
    report = await lookup.check_literature(
        _content("Мэнкью Н.Г. Принципы экономики. – СПб.: Питер, 2013."), _config()
    )
    assert report is not None and report.references[0].status == "not_found"


@pytest.mark.asyncio
async def test_unreachable_catalogue_is_cannot_verify(monkeypatch):
    def handler(terms: str, db: int) -> str | None:
        raise httpx.ConnectError("down")

    _fake_opac(monkeypatch, handler)
    report = await lookup.check_literature(
        _content("Мэнкью Н.Г. Принципы экономики. – СПб.: Питер, 2013."), _config()
    )
    assert report is not None and report.references[0].status == "error"
    assert lookup.criterion_result(SPEC, report, _config()).status == ResultStatus.cannot_verify


def test_status_thresholds():
    def report(*statuses: str) -> lookup.LibraryReport:
        return lookup.LibraryReport(
            provider="megapro_opac",
            references=[
                lookup.ReferenceResult(text=f"Title {i}", kind="core", status=s)
                for i, s in enumerate(statuses)
            ],  # type: ignore[arg-type]
        )

    cfg = _config()
    assert lookup.criterion_result(SPEC, report("held", "held"), cfg).status == ResultStatus.compliant
    assert (
        lookup.criterion_result(SPEC, report("held", "not_found", "no_copies"), cfg).status
        == ResultStatus.not_compliant
    )
    assert (
        lookup.criterion_result(SPEC, lookup.LibraryReport(provider="x", references=[]), cfg).status
        == ResultStatus.cannot_verify  # no list read is not the same as no list
    )


@pytest.mark.asyncio
async def test_pipeline_uses_the_report_instead_of_a_model():
    from app.ai import pipeline
    from app.ai.fake import FakeProvider

    report = lookup.LibraryReport(
        provider="megapro_opac",
        references=[
            lookup.ReferenceResult(
                text="Мэнкью Н.Г. Принципы экономики", kind="core", status="held", copies=3
            )
        ],
    )
    out = await pipeline.evaluate(
        _content("Мэнкью Н.Г. Принципы экономики"),
        [SPEC],
        FakeProvider(),
        decider=None,
        library_report=report,
        trace=False,
    )
    assert out.results[0].status == ResultStatus.compliant
    off = await pipeline.evaluate(_content("x"), [SPEC], FakeProvider(), decider=None, trace=False)
    assert off.results[0].status == ResultStatus.cannot_verify


def test_search_highlighting_does_not_split_words():
    page = _page(_record("104", "<strong>Принци</strong>пы <strong>эконом</strong>ики [Текст] : учебник"))
    assert megapro.parse_results(page)[1][0].description.startswith("Принципы экономики [Текст]")


@pytest.mark.asyncio
async def test_a_longer_book_sharing_two_stems_is_not_a_match(monkeypatch):
    other = _record(
        "105",
        "Котлер Филип. Маркетинг от А до Я. 80 концепций, которые должен знать каждый менеджер [Текст]",
        "Котлер Ф.",
    )
    right = _record(
        "106", "Котлер Ф. Маркетинг менеджмент [Текст] : учебник / Ф. Котлер, К.Л. Келлер.", "Котлер Ф."
    )
    _fake_opac(monkeypatch, lambda terms, db: _page(other, right))
    report = await lookup.check_literature(
        _content("Котлер Ф. Маркетинг менеджмент: учебник. – СПб.: Питер, 2018."), _config()
    )
    assert report is not None and report.references[0].record_id == "106"


def test_literature_from_the_template_table_row():
    from app.library.references import literature_lines

    raw = "\n".join(
        [
            "Course title | Digital Modeling",
            "Required or recommended literature and other learning resources/tools | Required literature:",
            "1. William Vaughan. Digital Modeling. New Riders, 2011.",
            "literature and other learning 2. Richard Williams. The Animator's Survival Kit. Faber & Faber, 2012.",
            "Learning resources and tools:",
            "Blender Manual - https://docs.blender.org/manual/",
            "Planned learning activities and teaching methods | Lecture-dialogue",
        ]
    )
    content = ExtractedContent(raw_text=raw, paragraphs=[])
    refs = split_references(literature_lines(content))
    assert [(r.name_words, r.title_words[:2]) for r in refs] == [
        (["William", "Vaughan"], ["Digital", "Modeling"]),
        (["Richard", "Williams"], ["Animator", "Survival"]),
    ]


def test_co_authors_two_letter_initials_and_titles_that_look_like_names():
    co = parse_reference("Гущина, О. М., Панюкова, Е. В. Цифровая культура : электронное учебное пособие.")
    assert co.surname == "Гущина" and co.title_words[:2] == ["Цифровая", "культура"]
    assert parse_reference("Коттер Дж. Впереди перемен. – М., 2020.").surname == "Коттер"
    title_first = parse_reference("Cybersecurity Audit Essentials Tools, Techniques. Salihu A. Apress, 2024.")
    assert title_first.name_words == [] and title_first.title_words[0] == "Cybersecurity"
    assert split_references(["ISO/IEC 27001:2022. Information security, cybersecurity and privacy."]) == []
