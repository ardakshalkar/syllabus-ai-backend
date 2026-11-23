"""Programs, learning outcomes and disciplines imported from narxoz.edu.kz (`python -m app.cli import-programs`)."""

from datetime import UTC, datetime

import sqlalchemy as sa

from app.db import pg
from app.models import Program, ProgramOutcome, School
from app.services import program_import as pi

TEXTS = {
    "ru": ["Применяет аналитические инструменты.", "Готовит отчётность&nbsp;по МСФО."],
    "kk": ["Аналитикалық құралдарды қолданады.", "ХҚЕС бойынша есептілік дайындайды."],
    "en": ["Applies analytical tools.", "Prepares IFRS statements."],
}


def detail(lang: str, *, code="6В04103", school="economics", outcomes=2, slug="finance-ba") -> dict:
    """The shape narxoz.kz/api/v1/education/programs/<slug>/ answers with, trimmed to what the import reads."""
    return {
        "id": 142,
        "slug": slug,
        "code": code,  # Cyrillic В, as the site often has it
        "school_slug": school,
        "levels": [{"slug": "bachelor"}],
        "title": {"ru": "Финансы", "kk": "Қаржы", "en": "Finance"}[lang],
        "goal": f"<p>goal {lang}</p>",
        # Listed out of order; ids are shared between languages.
        "learning_outcomes": [
            {"id": 300 + i, "sort_order": i + 1, "text": f"<p>{TEXTS[lang][i]}</p>"} for i in range(outcomes)
        ][::-1],
        "disciplines": [
            {"id": 1, "sort_order": 1, "is_profile_subject": False, "name": f"Statistics {lang}"},
            {"id": 2, "sort_order": 2, "is_profile_subject": True, "name": f"Corporate finance {lang}"},
            {"id": 3, "sort_order": 3, "is_profile_subject": True, "name": f"Corporate finance {lang}"},
        ],
    }


def site_program(**kw) -> pi.SiteProgram:
    return pi.parse_program({lang: detail(lang, **kw) for lang in pi.LANGS})


def test_parse_pairs_languages_by_id_and_normalises_the_code():
    p = site_program()
    assert p.code == "6B04103"
    assert p.degree_level == "Bachelor"
    assert p.titles == {"ru": "Финансы", "kk": "Қаржы", "en": "Finance"}
    assert p.outcomes[0] == {
        "ru": "Применяет аналитические инструменты.",
        "kk": "Аналитикалық құралдарды қолданады.",
        "en": "Applies analytical tools.",
    }
    assert p.outcomes[1]["ru"] == "Готовит отчётность по МСФО."  # HTML and &nbsp; gone
    # The duplicated discipline is listed once.
    assert [(n["en"], profile) for n, profile in p.disciplines] == [
        ("Statistics en", False),
        ("Corporate finance en", True),
    ]


def test_parse_keeps_an_outcome_missing_in_one_language():
    details = {lang: detail(lang) for lang in pi.LANGS}
    details["kk"]["learning_outcomes"] = [o for o in details["kk"]["learning_outcomes"] if o["id"] != 301]
    p = pi.parse_program(details)
    assert set(p.outcomes[0]) == {"ru", "kk", "en"}
    assert set(p.outcomes[1]) == {"ru", "en"}  # kk lacks outcome 2; it is still there


def test_normalize_code():
    assert pi.normalize_code(" 7М04124 ") == "7M04124"
    assert pi.normalize_code("8D04103") == "8D04103"


async def _economics(session) -> School:
    school = School(slug="economics-management", name="Школа экономики и менеджмента")
    session.add(school)
    await session.flush()
    return school


async def test_import_creates_then_updates_in_place(ref):
    async with pg.session_scope() as session:
        school = await _economics(session)
        report = await pi.import_programs(
            session,
            [
                site_program(),
                site_program(code="8D04106", school="", slug="finansy"),  # no School on the site
                site_program(code="6B99999", school="school-of-nowhere", slug="x"),
                site_program(code="6B04199", outcomes=0, slug="empty"),
            ],
        )
    assert report.created == ["6B04103", "8D04106"]
    assert report.school_inferred == [("8D04106", "economics-management")]
    assert [code for code, _ in report.skipped] == ["6B99999", "6B04199"]

    async with pg.session_scope() as session:
        program = await session.scalar(sa.select(Program).where(Program.code == "6B04103"))
        assert program.school_id == school.id and program.slug == "6b04103" and program.name == "Финансы"
        assert [o.number for o in program.outcomes] == [1, 2]
        ids = {o.number: o.id for o in program.outcomes}

    # The site drops outcome 2 and rewords outcome 1: the row is updated, not replaced.
    changed = {lang: detail(lang, outcomes=1) for lang in pi.LANGS}
    changed["en"]["learning_outcomes"][0]["text"] = "<p>Applies analytical tools to financial models.</p>"
    later = datetime(2026, 12, 1, tzinfo=UTC)
    async with pg.session_scope() as session:
        report = await pi.import_programs(session, [pi.parse_program(changed)], now=later)
    assert report.updated == ["6B04103"] and not report.created

    async with pg.session_scope() as session:
        program = await session.scalar(sa.select(Program).where(Program.code == "6B04103"))
        assert [(o.id, o.text["en"]) for o in program.outcomes] == [
            (ids[1], "Applies analytical tools to financial models.")
        ]
        assert program.imported_at == later
        assert await session.scalar(sa.select(sa.func.count(ProgramOutcome.id))) == 3  # 1 + 8D04106's 2


async def test_program_detail_api(ref, client_for):
    async with pg.session_scope() as session:
        await _economics(session)
        await pi.import_programs(session, [site_program()])
        program_id = await session.scalar(sa.select(Program.id).where(Program.code == "6B04103"))

    client = client_for(ref["users"]["kok"])
    listed = {p["code"]: p for p in (await client.get("/programs")).json()}
    assert listed["6B04103"]["outcome_count"] == 2
    assert listed["6B04103"]["site_url"] == "https://narxoz.edu.kz/program/finance-ba"

    got = await client.get(f"/programs/{program_id}")
    assert got.status_code == 200, got.text
    body = got.json()
    assert body["outcomes"][1] == {
        "number": 2,
        "text": {
            "ru": "Готовит отчётность по МСФО.",
            "kk": "ХҚЕС бойынша есептілік дайындайды.",
            "en": "Prepares IFRS statements.",
        },
    }
    assert [d["is_profile"] for d in body["disciplines"]] == [False, True]


async def _imported_id(code: str = "6B04103"):
    async with pg.session_scope() as session:
        await _economics(session)
        await pi.import_programs(session, [site_program()])
        return await session.scalar(sa.select(Program.id).where(Program.code == code))


async def test_outcomes_are_corrected_by_hand_and_survive_an_import(ref, client_for):
    program_id = await _imported_id()
    kok = client_for(ref["users"]["kok"])

    fixed = await kok.patch(
        f"/programs/{program_id}/outcomes/2",
        json={"text": {"ru": "  Готовит   отчётность. ", "kk": "", "en": "x"}},
    )
    assert fixed.status_code == 200, fixed.text
    body = fixed.json()
    assert body["outcomes"][1]["text"] == {
        "ru": "Готовит отчётность.",
        "en": "x",
    }  # trimmed, empty kk dropped
    assert body["outcomes_edited_at"]

    added = await kok.post(f"/programs/{program_id}/outcomes", json={"text": {"en": "Third."}})
    assert added.status_code == 201 and [o["number"] for o in added.json()["outcomes"]] == [1, 2, 3]

    # The site is imported again: the hand-edited outcomes stay, the rest of the program is refreshed.
    async with pg.session_scope() as session:
        report = await pi.import_programs(session, [site_program()])
    assert report.outcomes_kept == ["6B04103"] and report.outcomes == 0
    got = (await kok.get(f"/programs/{program_id}")).json()
    assert [o["text"].get("en") for o in got["outcomes"]] == ["Applies analytical tools.", "x", "Third."]

    # Restoring lets the next import bring the site's text back.
    restored = await kok.delete(f"/programs/{program_id}/outcome-edits")
    assert restored.status_code == 200 and restored.json()["outcomes_edited_at"] is None
    async with pg.session_scope() as session:
        report = await pi.import_programs(session, [site_program()])
    assert report.outcomes_kept == [] and report.outcomes == 2
    got = (await kok.get(f"/programs/{program_id}")).json()
    assert [o["number"] for o in got["outcomes"]] == [1, 2]


async def test_deleting_an_outcome_renumbers_the_rest_and_keeps_their_ids(ref, client_for):
    program_id = await _imported_id()
    kok = client_for(ref["users"]["kok"])
    await kok.post(f"/programs/{program_id}/outcomes", json={"text": {"en": "Third."}})
    async with pg.session_scope() as session:
        ids = {o.number: o.id for o in (await session.get(Program, program_id)).outcomes}

    done = await kok.delete(f"/programs/{program_id}/outcomes/1")
    assert done.status_code == 200, done.text
    assert [(o["number"], o["text"]["en"]) for o in done.json()["outcomes"]] == [
        (1, "Prepares IFRS statements."),
        (2, "Third."),
    ]
    async with pg.session_scope() as session:
        now = {o.number: o.id for o in (await session.get(Program, program_id)).outcomes}
    assert now == {1: ids[2], 2: ids[3]}
    assert (await kok.delete(f"/programs/{program_id}/outcomes/9")).status_code == 404


async def test_outcome_edits_need_text_and_the_right_role(ref, client_for):
    program_id = await _imported_id()
    kok = client_for(ref["users"]["kok"])
    assert (
        await kok.patch(f"/programs/{program_id}/outcomes/1", json={"text": {"en": "  "}})
    ).status_code == 422
    assert (
        await kok.patch(f"/programs/{program_id}/outcomes/1", json={"text": {"fr": "x"}})
    ).status_code == 422

    prof = client_for(ref["users"]["shalkar"])
    assert (
        await prof.post(f"/programs/{program_id}/outcomes", json={"text": {"en": "x"}})
    ).status_code == 403
    assert (await prof.delete(f"/programs/{program_id}/outcomes/1")).status_code == 403
    assert (await prof.get("/alignment/overview", params={"semester": "Fall 2026"})).status_code == 403


async def test_university_overview_lists_every_programs_coverage(ref, client_for):
    await _imported_id()
    kok = client_for(ref["users"]["kok"])
    got = await kok.get("/alignment/overview", params={"semester": "Fall 2026"})
    assert got.status_code == 200, got.text
    body = got.json()
    rows = [
        (s["school_name"], p["program"]["code"], p["outcome_count"], p["covered"])
        for s in body["schools"]
        for p in s["programs"]
    ]
    assert ("Школа экономики и менеджмента", "6B04103", 2, 0) in rows
    assert body["checked"] == 0
