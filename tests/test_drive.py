"""Files reach Drive after the upload, and the app survives Drive being down or the local copy being gone."""

import pytest
import sqlalchemy as sa

from app.auth.client import StubAuthClient
from app.db import pg
from app.models import SyllabusVersion
from app.services.drive_sync import sync_pending_files
from app.storage import drive, files
from seed.samples import sample_syllabus
from tests.conftest import DOCX, create_syllabus, upload


async def _upload(ref, client_for) -> tuple[dict, bytes]:
    lead = client_for(ref["users"]["shalkar"])
    syllabus = await create_syllabus(lead, ref["courses"]["ai"].id)
    data = sample_syllabus("Artificial Intelligence", with_issues=False)
    response = await lead.post(
        f"/syllabi/{syllabus['id']}/versions",
        files={"file": ("syllabus.docx", data, DOCX)},
        data={"folder_id": str(ref["folders"]["digital"].id)},
    )
    assert response.status_code == 201, response.text
    return response.json()["version"], data


async def _stored(version_id: str) -> SyllabusVersion:
    async with pg.session_scope() as session:
        found = await session.scalar(sa.select(SyllabusVersion).where(SyllabusVersion.id == version_id))
        assert found is not None
        return found


async def test_upload_reaches_the_configured_folder_once(ref, client_for):
    version, data = await _upload(ref, client_for)
    assert version["drive_synced_at"] is None

    assert await sync_pending_files() == 1
    stored = await _stored(version["id"])
    assert stored.drive_file_id.startswith(f"{ref['folders']['digital'].drive_folder_id}/")
    assert stored.drive_file_id.endswith("Artificial Intelligence Fall 2026 v1.docx")
    assert drive.get_drive().download(stored.drive_file_id) == data
    assert stored.drive_synced_at is not None and stored.drive_error is None

    assert await sync_pending_files() == 0


async def test_failed_copy_keeps_the_upload_and_retries(ref, client_for, monkeypatch):
    version, _ = await _upload(ref, client_for)

    def broken(self, *args) -> str:
        raise ConnectionError("Drive is down")

    with monkeypatch.context() as patch:
        patch.setattr(drive.StubDrive, "upload", broken)
        await sync_pending_files()
    stored = await _stored(version["id"])
    assert (stored.drive_file_id, stored.drive_error, stored.drive_attempts) == (None, "Drive is down", 1)

    lead = client_for(ref["users"]["shalkar"])
    run = (await lead.get(f"/versions/{version['id']}/runs/latest")).json()
    assert run["status"] == "Completed"

    await sync_pending_files()
    stored = await _stored(version["id"])
    assert stored.drive_file_id and stored.drive_error is None and stored.drive_attempts == 2


async def test_upload_without_a_folder_uses_the_school_folder(ref, client_for):
    lead = client_for(ref["users"]["shalkar"])
    syllabus = await create_syllabus(lead, ref["courses"]["ai"].id)
    uploaded = await upload(
        lead, syllabus["id"], sample_syllabus("Artificial Intelligence", with_issues=False)
    )

    await sync_pending_files()
    stored = await _stored(uploaded["version"]["id"])
    assert stored.folder_id == ref["folders"]["digital"].id
    assert stored.drive_file_id.startswith(f"{ref['folders']['digital'].drive_folder_id}/")


async def test_version_without_a_folder_waits(ref, client_for):
    lead = client_for(ref["users"]["shalkar"])
    syllabus = await create_syllabus(lead, ref["courses"]["ai"].id, semester="Spring 2030")
    uploaded = await upload(
        lead, syllabus["id"], sample_syllabus("Artificial Intelligence", with_issues=False)
    )

    await sync_pending_files()
    stored = await _stored(uploaded["version"]["id"])
    assert (stored.drive_file_id, stored.drive_error) == (None, "No Drive folder is configured")


async def test_lost_local_file_comes_back_from_drive(ref, client_for):
    version, data = await _upload(ref, client_for)
    await sync_pending_files()
    stored = await _stored(version["id"])
    files.absolute(stored.file_path).unlink()

    response = await client_for(ref["users"]["shalkar"]).get(f"/versions/{version['id']}/file")
    assert response.status_code == 200 and response.content == data
    assert files.absolute(stored.file_path).exists()


async def test_lost_local_file_before_the_copy_is_a_404(ref, client_for):
    version, _ = await _upload(ref, client_for)
    files.absolute((await _stored(version["id"])).file_path).unlink()

    response = await client_for(ref["users"]["shalkar"]).get(f"/versions/{version['id']}/file")
    assert response.status_code == 404


async def test_drive_returning_another_file_is_refused(ref, client_for, monkeypatch):
    version, _ = await _upload(ref, client_for)
    await sync_pending_files()
    stored = await _stored(version["id"])
    files.absolute(stored.file_path).unlink()
    monkeypatch.setattr(drive.StubDrive, "download", lambda self, file_id: b"something else")

    with pytest.raises(ValueError):
        await files.ensure_local(stored)
    assert not files.absolute(stored.file_path).exists()


async def test_folder_configuration(ref, client_for):
    admin = client_for(ref["users"]["admin.digital"])
    folder = ref["folders"]["digital"]

    updated = await admin.patch(f"/folders/{folder.id}", json={"drive_folder_id": " new-folder_1 "})
    assert updated.status_code == 200 and updated.json()["drive_folder_id"] == "new-folder_1"

    denied = await admin.patch(f"/folders/{folder.id}", json={"drive_folder_id": "../outside"})
    assert denied.status_code == 422

    renamed = await admin.patch(f"/folders/{folder.id}", json={"path": "Renamed"})
    assert renamed.json()["drive_folder_id"] == "new-folder_1"

    cleared = await admin.patch(f"/folders/{folder.id}", json={"drive_folder_id": None})
    assert cleared.json()["drive_folder_id"] is None and cleared.json()["path"] == "Renamed"

    created = await admin.post(
        f"/schools/{ref['schools']['digital'].id}/folders",
        json={"path": "New", "semester": "Fall 2026", "drive_folder_id": "abc"},
    )
    assert created.status_code == 201 and created.json()["drive_folder_id"] == "abc"


async def test_folder_reports_its_last_copy(ref, client_for):
    await _upload(ref, client_for)
    await sync_pending_files()
    admin = client_for(ref["users"]["admin.digital"])
    folders = (await admin.get(f"/schools/{ref['schools']['digital'].id}/folders")).json()
    assert folders[0]["last_synced_at"] is not None


async def test_refused_account_stores_nothing(ref, client_for, monkeypatch):
    async def refuse(self, email: str) -> bool:
        return False

    monkeypatch.setattr(StubAuthClient, "verify_account", refuse)
    lead = client_for(ref["users"]["shalkar"])
    syllabus = await create_syllabus(lead, ref["courses"]["ai"].id)
    response = await lead.post(
        f"/syllabi/{syllabus['id']}/versions",
        files={"file": ("s.docx", sample_syllabus("Artificial Intelligence", with_issues=False), DOCX)},
    )
    assert response.status_code == 403
    assert (await lead.get(f"/syllabi/{syllabus['id']}/versions")).json() == []
