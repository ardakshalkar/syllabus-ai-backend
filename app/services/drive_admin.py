"""Connecting Google Drive from the app (Schools & Drive screen, `/drive` API, `drive-check` CLI):
save the service account key, set the shared folder, and check that everything works.

The key is written to SECRETS_ROOT with mode 600 and is never sent back: the API only shows the account's
email and project, which is what people need to share folders with it.
"""

import asyncio
import json
import logging
import os
import re
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import sqlalchemy as sa

from app.db.pg import session_scope
from app.documents import DriveSettingsDoc
from app.models import UploadFolder
from app.storage import drive_config
from app.storage.drive import GoogleDrive, StubDrive, explain_error, load_credentials

log = logging.getLogger(__name__)

MAX_KEY_BYTES = 64 * 1024
_GOOGLE_ID = re.compile(r"[\w-]{10,}")
_EMAIL = re.compile(r"[^@\s]+@[^@\s]+\.[^@\s]+")
_FOLDER = "application/vnd.google-apps.folder"


class DriveSettingsError(ValueError):
    """The key or a setting cannot be used; the message says why (shown in the UI)."""


async def _doc() -> DriveSettingsDoc:
    doc = await DriveSettingsDoc.find_one()
    if doc is None:
        doc = DriveSettingsDoc(read_only=drive_config.from_env().read_only)
    return doc


async def _save(doc: DriveSettingsDoc, by: str) -> None:
    doc.updated_by = by
    doc.updated_at = datetime.now(UTC)
    await doc.save()
    await drive_config.refresh()


def _parse_key(data: bytes) -> dict[str, Any]:
    if len(data) > MAX_KEY_BYTES:
        raise DriveSettingsError("This file is too large to be a Google key (JSON, a few KB)")
    try:
        info = json.loads(data)
    except ValueError as exc:
        raise DriveSettingsError("The file is not JSON: download the key again as JSON") from exc
    if not isinstance(info, dict):
        raise DriveSettingsError("The file is not a Google key")
    kind = info.get("type")
    if kind == "service_account":
        missing = [f for f in ("client_email", "private_key", "token_uri") if not info.get(f)]
        if missing:
            raise DriveSettingsError(f"The service account key has no {', '.join(missing)}")
        from google.oauth2 import service_account

        try:
            service_account.Credentials.from_service_account_info(info)
        except ValueError as exc:
            raise DriveSettingsError(f"The private key in this file cannot be read: {exc}") from exc
    elif kind == "authorized_user":
        missing = [f for f in ("client_id", "client_secret", "refresh_token") if not info.get(f)]
        if missing:
            raise DriveSettingsError(f"The authorized-user file has no {', '.join(missing)}")
    else:
        raise DriveSettingsError(
            "Expected a service account key (type service_account) or an authorized-user file; "
            f"this file has type {kind!r}"
        )
    return info


def _write_secret(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".key-")
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "wb") as out:
            out.write(data)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


async def save_key(data: bytes, by: str) -> DriveSettingsDoc:
    """Use this key for Google Drive from now on (switches the app to Google Drive)."""
    info = _parse_key(data)
    doc = await _doc()
    if info["type"] != "service_account" and doc.impersonate_user:
        raise DriveSettingsError(
            "Acting as a Workspace user needs a service account key: clear that field first"
        )
    await asyncio.to_thread(_write_secret, drive_config.key_path(), data)
    doc.credentials_saved = True
    doc.credentials_type = info["type"]
    doc.client_email = info.get("client_email")
    doc.client_id = info.get("client_id")
    doc.project_id = info.get("project_id")
    await _save(doc, by)
    return doc


async def forget_key(by: str) -> DriveSettingsDoc:
    """Delete the uploaded key: Drive goes back to what .env says."""
    doc = await _doc()
    await asyncio.to_thread(drive_config.key_path().unlink, missing_ok=True)
    doc.credentials_saved = False
    doc.credentials_type = doc.client_email = doc.client_id = doc.project_id = None
    await _save(doc, by)
    return doc


def folder_id_from(text: str) -> str:
    """A folder id, also from a pasted link (…/drive/folders/<id>?usp=sharing)."""
    text = text.strip()
    if "/folders/" in text:
        text = text.split("/folders/", 1)[1].split("?", 1)[0].strip("/")
    return text


async def update_settings(
    by: str,
    *,
    shared_folder_id: str | None = None,
    impersonate_user: str | None = None,
    read_only: bool | None = None,
) -> DriveSettingsDoc:
    doc = await _doc()
    if shared_folder_id is not None:
        folder = folder_id_from(shared_folder_id)
        stub = not doc.credentials_saved and drive_config.from_env().provider == "stub"
        if folder and not stub and not _GOOGLE_ID.fullmatch(folder):
            raise DriveSettingsError("This is not a Drive folder id: copy the last part of the folder's link")
        if folder and stub and ".." in folder:
            raise DriveSettingsError("Not a folder under the local Drive stub")
        doc.shared_folder_id = folder
    if impersonate_user is not None:
        email = impersonate_user.strip()
        if email and not _EMAIL.fullmatch(email):
            raise DriveSettingsError("Acting as: enter an email address or leave it empty")
        if email and doc.credentials_saved and doc.credentials_type != "service_account":
            raise DriveSettingsError("Acting as a Workspace user needs a service account key")
        doc.impersonate_user = email
    if read_only is not None:
        doc.read_only = read_only
    await _save(doc, by)
    return doc


def _public_fields(path: Path | None) -> dict[str, Any]:
    """The non-secret fields of the key from .env, so the UI can show whom to share folders with."""
    if path is None or not path.is_file() or path.stat().st_size > MAX_KEY_BYTES:
        return {}
    try:
        info = json.loads(path.read_text())
    except (OSError, ValueError):
        return {}
    if not isinstance(info, dict):
        return {}
    return {k: info.get(k) for k in ("type", "client_email", "client_id", "project_id")}


async def status() -> dict[str, Any]:
    doc = await DriveSettingsDoc.find_one()
    config = await drive_config.refresh()
    if doc and doc.credentials_saved:
        key = {
            "type": doc.credentials_type,
            "client_email": doc.client_email,
            "client_id": doc.client_id,
            "project_id": doc.project_id,
        }
    else:
        key = await asyncio.to_thread(_public_fields, config.credentials_file)
    return {
        "provider": config.provider,
        "source": config.source,
        "credentials_type": key.get("type"),
        "client_email": key.get("client_email"),
        "client_id": key.get("client_id"),
        "project_id": key.get("project_id"),
        "env_credentials": bool(config.source == "env" and config.credentials_file),
        "impersonate_user": config.impersonate_user,
        "shared_folder_id": config.shared_folder_id,
        "default_folder_id": config.default_folder_id,
        "read_only": config.read_only,
        "updated_by": doc.updated_by if doc else None,
        "updated_at": doc.updated_at if doc else None,
    }


async def folders_to_check(given: list[str] | None = None) -> list[str]:
    """The folders a check looks at: the ones given, else the shared folder, the default and every School's."""
    if given:
        return list(dict.fromkeys(folder_id_from(f) for f in given if f.strip()))
    config = await drive_config.refresh()
    ids = [config.shared_folder_id, config.default_folder_id]
    try:
        async with session_scope() as session:
            ids += list(
                await session.scalars(
                    sa.select(UploadFolder.drive_folder_id).where(UploadFolder.drive_folder_id.is_not(None))
                )
            )
    except Exception:  # the check is still useful without the database
        pass
    return [f for f in dict.fromkeys(ids) if f][:20]


def _check_folder(client, config: drive_config.DriveConfig, folder_id: str, write: bool, is_sa: bool) -> dict:
    result: dict[str, Any] = {
        "id": folder_id,
        "ok": False,
        "error": None,
        "name": None,
        "location": None,
        "can_upload": None,
        "items": None,
        "sample": [],
        "warnings": [],
        "write_test": None,
    }
    try:
        if isinstance(client, GoogleDrive):
            meta = client.folder_info(folder_id)
            if meta.get("mimeType") != _FOLDER:
                result["error"] = f"'{meta.get('name')}' is a file, not a folder"
                return result
            result["name"] = meta.get("name")
            shared = bool(meta.get("driveId"))
            result["location"] = "Shared Drive" if shared else "My Drive"
            result["can_upload"] = bool((meta.get("capabilities") or {}).get("canAddChildren"))
            if is_sa and not shared and not config.impersonate_user:
                result["warnings"].append(
                    (
                        "Reading works, but uploads will fail once read-only is off"
                        if config.read_only
                        else "Uploads will fail"
                    )
                    + ": a service account has no storage of its own. Use a folder in a Shared Drive, or act as "
                    "a Workspace user (domain-wide delegation)."
                )
        else:
            result["name"] = folder_id.rsplit("/", 1)[-1]
            result["location"] = "Local stub"
            result["can_upload"] = True
        entries = client.list_folder(folder_id)
    except Exception as exc:
        result["error"] = explain_error(exc)
        return result
    result["items"] = len(entries)
    result["sample"] = [e.name for e in entries[:5]]
    result["ok"] = True
    if write:
        if config.read_only:
            result["write_test"] = "skipped: Drive is read-only"
        else:
            try:
                file_id = client.upload(
                    folder_id, "syllabus-ai drive-check.txt", b"test", "text/plain", "drive-check"
                )
                if isinstance(client, GoogleDrive):
                    client.delete(file_id)
                result["write_test"] = "ok"
            except Exception as exc:
                result["write_test"] = f"failed: {explain_error(exc)}"
                result["ok"] = False
    return result


def _check_sync(config: drive_config.DriveConfig, folder_ids: list[str], write: bool) -> dict[str, Any]:
    account: dict[str, Any] = {"ok": True, "error": None, "type": None, "email": None, "project_id": None}
    is_sa = False
    if config.provider == "google":
        path = Path(config.credentials_file or "")
        try:
            load_credentials(path, read_only=config.read_only, impersonate=config.impersonate_user)
            info = json.loads(path.read_text())
            is_sa = info.get("type") == "service_account"
            account.update(
                type=info.get("type"), email=info.get("client_email"), project_id=info.get("project_id")
            )
        except Exception as exc:
            account.update(ok=False, error=str(exc))
    else:
        account["type"] = "stub"
    if config.impersonate_user:
        acting_as = config.impersonate_user
    elif config.provider == "google":
        acting_as = "the service account itself" if is_sa else "the authorized user"
    else:
        acting_as = "local stub"
    account["acting_as"] = acting_as
    folders: list[dict] = []
    if account["ok"]:
        client = GoogleDrive() if config.provider == "google" else StubDrive()
        folders = [_check_folder(client, config, f, write, is_sa) for f in folder_ids]
    return {
        "provider": config.provider,
        "source": config.source,
        "read_only": config.read_only,
        "account": account,
        "folders": folders,
        "ok": account["ok"] and all(f["ok"] for f in folders),
        "checked_at": datetime.now(UTC),
    }


async def check(folder_ids: list[str] | None = None, write: bool = False) -> dict[str, Any]:
    """Check the credentials and each folder (read, and with `write` a test upload). Never raises for Drive
    problems: they are reported per item, for the UI and `drive-check`."""
    config = await drive_config.refresh()
    ids = await folders_to_check(folder_ids)
    return await asyncio.to_thread(_check_sync, config, ids, write)
