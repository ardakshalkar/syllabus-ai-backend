"""`drive-setup`: one shared Drive folder becomes the upload folder of every School (DRIVE_PROVIDER=stub)."""

import uuid

import sqlalchemy as sa

from app.config import get_settings
from app.db import pg
from app.models import UploadFolder
from app.services.drive_setup import setup_shared_folder
from app.services.drive_sync import sync_pending_files
from seed.samples import sample_syllabus
from tests.conftest import create_syllabus, upload

SEMESTER = "Fall 2026"


def _shared_folder(*school_folders: str) -> str:
    """A fresh shared folder under the Drive stub root (shared by all tests) holding these School folders."""
    name = f"shared-{uuid.uuid4().hex[:8]}"
    root = get_settings().drive_stub_root / name
    root.mkdir(parents=True)
    for school in school_folders:
        (root / school).mkdir()
    return name


async def _drive_ids() -> dict[str, str | None]:
    async with pg.session_scope() as session:
        rows = await session.execute(
            sa.select(UploadFolder.path, UploadFolder.drive_folder_id).where(
                UploadFolder.semester == SEMESTER
            )
        )
        return dict(rows.all())


async def test_dry_run_changes_nothing(ref):
    root = _shared_folder("school of digital technologies")  # matched regardless of case
    before = await _drive_ids()

    report = await setup_shared_folder(root, SEMESTER, dry_run=True)

    actions = {link.school: link.action for link in report.links}
    assert actions["School of Digital Technologies"] == "found"
    assert actions["School of Economics and Management"] == "would create"
    assert await _drive_ids() == before
    assert not (get_settings().drive_stub_root / root / "School of Economics and Management").exists()


async def test_every_school_gets_its_folder_and_uploads_land_there(ref, client_for):
    root = _shared_folder("School of Digital Technologies")

    report = await setup_shared_folder(root, SEMESTER)

    actions = {link.school: link.action for link in report.links}
    assert actions == {
        "School of Arts and Social Sciences": "created",
        "School of Digital Technologies": "found",
        "School of Economics and Management": "created",
    }
    ids = await _drive_ids()
    assert ids["Academic / Syllabi / 2026 / Fall"] == f"{root}/School of Digital Technologies"
    assert ids["Syllabi / Fall-2026"] == f"{root}/School of Economics and Management"
    assert (get_settings().drive_stub_root / root / "School of Arts and Social Sciences").is_dir()

    lead = client_for(ref["users"]["shalkar"])
    syllabus = await create_syllabus(lead, ref["courses"]["ai"].id)
    await upload(lead, syllabus["id"], sample_syllabus("ai", with_issues=False))
    assert await sync_pending_files() == 1
    copied = list((get_settings().drive_stub_root / root / "School of Digital Technologies").iterdir())
    assert len(copied) == 1

    again = await setup_shared_folder(root, SEMESTER)  # running it twice changes nothing
    assert {link.action for link in again.links} == {"found"}
    assert again.folders_updated_in_db == 0 and again.folders_created_in_db == 0


async def test_a_school_without_an_upload_folder_for_the_semester_gets_one(ref):
    root = _shared_folder()

    report = await setup_shared_folder(root, "Spring 2027")

    assert report.folders_created_in_db == 3
    async with pg.session_scope() as session:
        paths = set(
            await session.scalars(sa.select(UploadFolder.path).where(UploadFolder.semester == "Spring 2027"))
        )
    assert "School of Digital Technologies" in paths


async def test_read_only_links_existing_folders_and_creates_none(ref, monkeypatch):
    monkeypatch.setenv("DRIVE_READ_ONLY", "true")
    get_settings.cache_clear()
    try:
        root = _shared_folder("School of Digital Technologies")
        report = await setup_shared_folder(root, SEMESTER)
    finally:
        monkeypatch.setenv("DRIVE_READ_ONLY", "false")
        get_settings.cache_clear()

    actions = {link.school: link.action for link in report.links}
    assert actions["School of Digital Technologies"] == "found"
    assert actions["School of Arts and Social Sciences"] == "missing"
    assert not (get_settings().drive_stub_root / root / "School of Arts and Social Sciences").exists()
