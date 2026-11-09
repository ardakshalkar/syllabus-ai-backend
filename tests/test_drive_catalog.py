"""Syllabi read from Google Drive without importing them (services/drive_catalog.py), with the Drive stub:
a School folder holds discipline folders, they hold the syllabus files."""

import os
import uuid

import sqlalchemy as sa

from app.config import get_settings
from app.db import pg
from app.enums import AiStatus, ApprovalStage, RunStatus, VersionSource
from app.models import AssessmentRun, Course, Syllabus, SyllabusVersion, UploadFolder
from app.services import checks, drive_catalog
from seed.samples import sample_syllabus


def _school_folder() -> str:
    """<root>/<discipline>/<files>, like the School of Digital Technologies' "Осень 2026-2027 силлабусы"."""
    name = f"school-{uuid.uuid4().hex[:8]}"
    root = get_settings().drive_stub_root / name
    (root / "Алгоритмизация и программирование").mkdir(parents=True)
    (root / "Алгоритмизация и программирование" / "Силлабус RU.docx").write_bytes(
        sample_syllabus(f"{name} algo", with_issues=True)
    )
    (root / "Физика" / "2026").mkdir(parents=True)  # a syllabus one level deeper still counts
    (root / "Физика" / "2026" / "Physics EN.docx").write_bytes(
        sample_syllabus(f"{name} physics", with_issues=False)
    )
    (root / "Физика" / "notes.txt").write_text("not a syllabus")
    (root / "readme.docx").write_bytes(b"outside any discipline")
    return name


async def _link(ref, root: str) -> uuid.UUID:
    """Point the School of Digital Technologies' Fall 2026 folder at the stub folder."""
    folder = ref["folders"]["digital"]
    async with pg.session_scope() as session:
        await session.execute(
            sa.update(UploadFolder).where(UploadFolder.id == folder.id).values(drive_folder_id=root)
        )
    return folder.id


def _touch(path, seconds: int) -> None:
    """A new Drive revision: the stub's version is the file's mtime."""
    stat = path.stat()
    os.utime(path, ns=(stat.st_atime_ns, stat.st_mtime_ns + seconds * 1_000_000_000))


async def _drive_syllabi() -> list[tuple[Syllabus, Course]]:
    async with pg.session_scope() as session:
        rows = (
            await session.execute(
                sa.select(Syllabus, Course)
                .join(Course)
                .where(Course.slug.like("drive-%"))
                .order_by(Course.name)
            )
        ).all()
        return [(s, c) for s, c in rows]


async def test_sync_catalogues_the_files_without_storing_them(ref):
    root = _school_folder()
    folder_id = await _link(ref, root)

    report = await drive_catalog.sync_folder(folder_id)

    assert report.error is None, report.error
    assert (report.disciplines, report.files, report.added) == (2, 2, 2)
    assert {s["path"] for s in report.skipped} == {"readme.docx", "Физика/notes.txt"}
    found = await _drive_syllabi()
    assert [c.name for _, c in found] == [
        "Алгоритмизация и программирование · Силлабус RU",
        "Физика · Physics EN",
    ]
    assert {c.discipline for _, c in found} == {"Алгоритмизация и программирование", "Физика"}
    assert all(c.lead_id is None and s.professor_id is None for s, c in found)
    async with pg.session_scope() as session:
        versions = list(
            await session.scalars(
                sa.select(SyllabusVersion).where(SyllabusVersion.source == VersionSource.drive)
            )
        )
    assert len(versions) == 2 and all(v.file_path is None and v.drive_version for v in versions)
    assert not any((get_settings().files_root / str(s.id)).exists() for s, _ in found)  # nothing stored

    again = await drive_catalog.sync_folder(folder_id)
    assert (again.added, again.updated, again.unchanged) == (0, 0, 2)


async def test_a_changed_file_becomes_a_new_version_and_a_removed_one_is_hidden(ref, client_for):
    root = _school_folder()
    folder_id = await _link(ref, root)
    await drive_catalog.sync_folder(folder_id)
    algo = get_settings().drive_stub_root / root / "Алгоритмизация и программирование" / "Силлабус RU.docx"
    _touch(algo, 60)
    (get_settings().drive_stub_root / root / "Физика" / "2026" / "Physics EN.docx").unlink()

    report = await drive_catalog.sync_folder(folder_id)

    assert (report.updated, report.missing) == (1, 1)
    provost = client_for(ref["users"]["provost"])
    listed = (
        await provost.get("/syllabi", params={"discipline": "Алгоритмизация и программирование"})
    ).json()
    item = listed["items"][0]
    assert item["current_version"] == 2 and item["in_drive"] and item["approval_stage"] == ApprovalStage.draft
    everything = (await provost.get("/syllabi", params={"q": "Physics"})).json()["items"]
    assert everything == []  # left Drive: hidden
    missing = (await provost.get("/syllabi", params={"q": "Physics", "include_missing": True})).json()[
        "items"
    ]
    assert missing[0]["drive_missing"] is True


async def test_a_school_admin_checks_a_syllabus_straight_from_drive(ref, client_for):
    root = _school_folder()
    await drive_catalog.sync_folder(await _link(ref, root))
    syllabus, _ = (await _drive_syllabi())[0]
    admin = client_for(ref["users"]["admin.digital"])
    version = (await admin.get(f"/syllabi/{syllabus.id}/versions")).json()[0]

    run = await admin.post(f"/versions/{version['id']}/runs")  # TASKS_EAGER: done when it returns

    assert run.status_code == 201, run.text
    detail = (await admin.get(f"/syllabi/{syllabus.id}")).json()
    assert detail["ai_status"] == AiStatus.needs_revision and detail["language"] != "Unknown"
    assert not (get_settings().files_root / str(syllabus.id)).exists()  # read from Drive, not stored
    assert not any(get_settings().tmp_root.glob("drive-*"))  # the temporary copy is gone
    file = await admin.get(f"/versions/{version['id']}/file")
    assert file.status_code == 200 and file.content[:2] == b"PK"  # read from Drive for the download

    economics_admin = client_for(type("U", (), {"email": "nobody@demo.narxoz.kz"})())
    assert (await economics_admin.post(f"/versions/{version['id']}/runs")).status_code in (403, 404)


async def test_a_check_reads_the_newest_revision_in_drive(ref, client_for):
    root = _school_folder()
    await drive_catalog.sync_folder(await _link(ref, root))
    syllabus, _ = (await _drive_syllabi())[0]
    async with pg.session_scope() as session:
        old = await session.scalar(
            sa.select(SyllabusVersion).where(SyllabusVersion.syllabus_id == syllabus.id)
        )
        run = await checks.create_run(session, old, trigger=checks.Trigger.professor_manual)
        run_id = run.id
    _touch(
        get_settings().drive_stub_root / root / "Алгоритмизация и программирование" / "Силлабус RU.docx", 120
    )

    await checks.execute_run(run_id)  # edited in Drive after the last sync

    async with pg.session_scope() as session:
        run = await session.get(AssessmentRun, run_id)
        version = await session.get(SyllabusVersion, run.version_id)
    assert run.status == RunStatus.completed and version.version == 2


async def test_a_file_gone_from_drive_fails_its_check_with_a_reason(ref):
    root = _school_folder()
    await drive_catalog.sync_folder(await _link(ref, root))
    syllabus, _ = (await _drive_syllabi())[0]
    async with pg.session_scope() as session:
        version = await session.scalar(
            sa.select(SyllabusVersion).where(SyllabusVersion.syllabus_id == syllabus.id)
        )
        run_id = (await checks.create_run(session, version, trigger=checks.Trigger.professor_manual)).id
    (
        get_settings().drive_stub_root / root / "Алгоритмизация и программирование" / "Силлабус RU.docx"
    ).unlink()

    await checks.execute_run(run_id)

    async with pg.session_scope() as session:
        run = await session.get(AssessmentRun, run_id)
        assert run.status == RunStatus.failed and "no longer in Google Drive" in run.error
        assert (await session.get(Syllabus, syllabus.id)).drive_missing_at is not None


async def test_the_school_submits_a_course_without_a_lead(ref, client_for):
    root = _school_folder()
    await drive_catalog.sync_folder(await _link(ref, root))
    syllabus, _ = (await _drive_syllabi())[0]
    admin = client_for(ref["users"]["admin.digital"])
    version = (await admin.get(f"/syllabi/{syllabus.id}/versions")).json()[0]
    await admin.post(f"/versions/{version['id']}/runs")

    options = (await admin.get(f"/syllabi/{syllabus.id}")).json()["available_transitions"]
    assert [o["to_stage"] for o in options] == [ApprovalStage.head_review]
    moved = await admin.post(
        f"/syllabi/{syllabus.id}/transitions", json={"to_stage": ApprovalStage.head_review}
    )
    assert moved.status_code in (200, 201), moved.text
    head = client_for(ref["users"]["head.cs"])  # Head of the School's (first) Program
    assert any("Head of Program review" in n["title"] for n in (await head.get("/notifications")).json())
    returned = await head.post(
        f"/syllabi/{syllabus.id}/transitions",
        json={"to_stage": ApprovalStage.professor_revised, "comment": "Fix LO"},
    )
    assert returned.status_code in (200, 201), returned.text
    assert any("returned" in n["title"] for n in (await admin.get("/notifications")).json())


async def test_sync_api_respects_the_schools(ref, client_for):
    root = _school_folder()
    await _link(ref, root)
    admin = client_for(ref["users"]["admin.digital"])
    digital, economics = ref["schools"]["digital"].id, ref["schools"]["economics"].id

    assert (await admin.post("/drive/sync", json={"school_id": str(economics)})).status_code == 403
    assert (await admin.post("/drive/sync", json={})).status_code == 403
    assert (await admin.post("/drive/sync", json={"school_id": str(digital)})).status_code == 202

    status = (await admin.get("/drive/sync")).json()
    mine = [f for f in status["folders"] if f["drive_folder_id"] == root]
    assert mine and mine[0]["syllabi"] == 2 and mine[0]["last_sync"]["files"] == 2
    assert all(f["school_id"] == str(digital) for f in status["folders"])  # only their School
    kok = client_for(ref["users"]["kok"])
    assert (await kok.post("/drive/sync", json={})).status_code == 202
