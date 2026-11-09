"""Connecting Google Drive from the app: key upload, settings, checks and folder setup (`/drive` API).
Google is never called: the Drive client's network methods are replaced where a test needs them."""

import json
import stat
import uuid

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from app.config import get_settings
from app.storage import drive_config
from app.storage.drive import GoogleDrive

SA_EMAIL = "syllabus-ai@narxoz-test.iam.gserviceaccount.com"


@pytest.fixture(scope="module")
def service_account_key() -> bytes:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = key.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
    ).decode()
    return json.dumps(
        {
            "type": "service_account",
            "project_id": "narxoz-test",
            "private_key_id": "abc",
            "private_key": pem,
            "client_email": SA_EMAIL,
            "client_id": "1234567890",
            "token_uri": "https://oauth2.googleapis.com/token",
        }
    ).encode()


async def _upload_key(client, data: bytes):
    return await client.post("/drive/credentials", files={"file": ("key.json", data, "application/json")})


async def test_only_the_provost_office_connects_drive(ref, client_for, service_account_key):
    admin = client_for(ref["users"]["admin.digital"])
    assert (await admin.get("/drive")).status_code == 200
    assert (await _upload_key(admin, service_account_key)).status_code == 403
    assert (await admin.post("/drive/check", json={})).status_code == 403
    professor = client_for(ref["users"]["shalkar"])
    assert (await professor.get("/drive")).status_code == 403


@pytest.mark.parametrize(
    ("data", "message"),
    [
        (b"not json", "not JSON"),
        (json.dumps({"type": "external_account"}).encode(), "Expected a service account key"),
        (json.dumps({"type": "service_account", "client_email": "x@y.z"}).encode(), "has no private_key"),
        (
            json.dumps(
                {"type": "service_account", "client_email": "x@y.z", "private_key": "nope", "token_uri": "t"}
            ).encode(),
            "cannot be read",
        ),
    ],
)
async def test_unusable_key_is_refused_with_a_reason(ref, client_for, data, message):
    provost = client_for(ref["users"]["provost"])
    response = await _upload_key(provost, data)
    assert response.status_code == 422
    assert message in response.json()["detail"]
    assert (await provost.get("/drive")).json()["provider"] == "stub"


async def test_uploaded_key_switches_the_app_to_google_drive(ref, client_for, service_account_key):
    provost = client_for(ref["users"]["provost"])

    response = await _upload_key(provost, service_account_key)

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["provider"] == "google" and body["source"] == "app"
    assert body["client_email"] == SA_EMAIL and body["client_id"] == "1234567890"
    assert body["updated_by"] == ref["users"]["provost"].email
    assert "private_key" not in response.text
    key_file = get_settings().secrets_root / drive_config.KEY_FILENAME
    assert key_file.read_bytes() == service_account_key
    assert stat.S_IMODE(key_file.stat().st_mode) == 0o600
    assert (await provost.get("/integrations")).json()["drive"] == "google"

    deleted = await provost.delete("/drive/credentials")
    assert deleted.json()["provider"] == "stub" and deleted.json()["client_email"] is None
    assert not key_file.exists()


async def test_settings_take_a_folder_link_and_validate(ref, client_for, service_account_key):
    provost = client_for(ref["users"]["provost"])
    await _upload_key(provost, service_account_key)

    link = "https://drive.google.com/drive/folders/1AbCdEfGhIjKlMnOpQ?usp=sharing"
    saved = await provost.patch(
        "/drive", json={"shared_folder_id": link, "impersonate_user": "schools@narxoz.kz", "read_only": False}
    )
    assert saved.status_code == 200, saved.text
    assert saved.json()["shared_folder_id"] == "1AbCdEfGhIjKlMnOpQ"
    assert saved.json()["default_folder_id"] == "1AbCdEfGhIjKlMnOpQ"
    assert saved.json()["impersonate_user"] == "schools@narxoz.kz"
    assert saved.json()["read_only"] is False

    assert (await provost.patch("/drive", json={"shared_folder_id": "my folder"})).status_code == 422
    assert (await provost.patch("/drive", json={"impersonate_user": "not-an-email"})).status_code == 422
    await provost.delete("/drive/credentials")


async def test_check_reports_the_account_and_each_folder(ref, client_for, service_account_key, monkeypatch):
    provost = client_for(ref["users"]["provost"])
    await _upload_key(provost, service_account_key)
    await provost.patch("/drive", json={"shared_folder_id": "1SharedFolderId", "read_only": False})
    folders = {
        "1SharedFolderId": {
            "mimeType": "application/vnd.google-apps.folder",
            "name": "Syllabi",
            "capabilities": {"canAddChildren": True},
        },
    }

    def folder_info(self, folder_id):
        if folder_id not in folders:
            import httplib2
            from googleapiclient.errors import HttpError

            raise HttpError(
                httplib2.Response({"status": 404}), b'{"error": {"errors": [{"reason": "notFound"}]}}'
            )
        return folders[folder_id]

    monkeypatch.setattr(GoogleDrive, "folder_info", folder_info)
    monkeypatch.setattr(GoogleDrive, "list_folder", lambda self, folder_id: [])

    result = (
        await provost.post("/drive/check", json={"folder_ids": ["1SharedFolderId", "1MissingFolder"]})
    ).json()

    assert result["account"]["ok"] and result["account"]["email"] == SA_EMAIL
    assert result["account"]["acting_as"] == "the service account itself"
    shared, missing = result["folders"]
    assert shared["ok"] and shared["name"] == "Syllabi" and shared["location"] == "My Drive"
    assert "no storage of its own" in shared["warnings"][0]  # My Drive + service account + writes on
    assert not missing["ok"] and "share it with the service account" in missing["error"]
    assert result["ok"] is False
    await provost.delete("/drive/credentials")


async def test_without_a_key_the_local_stub_is_checked_and_set_up(ref, client_for):
    root = f"shared-{uuid.uuid4().hex[:8]}"
    (get_settings().drive_stub_root / root / "School of Digital Technologies").mkdir(parents=True)
    provost = client_for(ref["users"]["provost"])
    assert (
        await provost.post("/drive/setup", json={"semester": "Fall 2026"})
    ).status_code == 422  # no folder yet
    await provost.patch("/drive", json={"shared_folder_id": root, "read_only": False})

    check = (await provost.post("/drive/check", json={"write": True})).json()
    shared = next(f for f in check["folders"] if f["id"] == root)
    assert check["provider"] == "stub" and shared["ok"] and shared["write_test"] == "ok"

    preview = (await provost.post("/drive/setup", json={"semester": "Fall 2026", "dry_run": True})).json()
    actions = {link["school"]: link["action"] for link in preview["links"]}
    assert actions["School of Digital Technologies"] == "found"
    assert actions["School of Arts and Social Sciences"] == "would create"

    applied = (await provost.post("/drive/setup", json={"semester": "Fall 2026"})).json()
    assert applied["folders_updated"] >= 1
    folders = (await provost.get(f"/schools/{ref['schools']['digital'].id}/folders")).json()
    assert folders[0]["drive_folder_id"] == f"{root}/School of Digital Technologies"
