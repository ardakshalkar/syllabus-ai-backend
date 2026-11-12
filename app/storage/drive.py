"""Google Drive holds the durable copy of every syllabus version (see services/drive_sync.py), and the syllabi
that already exist there are read in place (see services/drive_catalog.py).

DRIVE_PROVIDER=stub is the default: StubDrive treats the folders under DRIVE_STUB_ROOT as Drive, a folder id
being its path relative to that root. Point the root at a downloaded copy of the real Drive folder to test the
import with real files before a Google account is connected. DRIVE_PROVIDER=google reads and writes the real
Drive with the credentials in GOOGLE_CREDENTIALS_FILE.
"""

import io
import json
import re
import unicodedata
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol

from app.config import get_settings
from app.storage import drive_config

_FOLDER_ID = re.compile(r"[\w-]+")
_DRIVE_ID = re.compile(r"[\w-]+")

DOCX = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
PDF = "application/pdf"
_GOOGLE_FOLDER = "application/vnd.google-apps.folder"
_GOOGLE_DOC = "application/vnd.google-apps.document"
_BY_SUFFIX = {".docx": DOCX, ".pdf": PDF}


@dataclass(frozen=True)
class DriveEntry:
    id: str
    name: str
    is_folder: bool
    # Content type the file downloads as; None for folders and anything else.
    content_type: str | None = None
    # Drive's revision counter: it grows whenever the file changes (Google Docs included).
    version: str | None = None
    modified_at: datetime | None = None
    size: int | None = None
    # Set on the files this app copied into Drive (services/drive_sync.py): the version they hold.
    app_version_id: str | None = None
    # The folder it was listed in (list_children).
    parent_id: str | None = None


def nfc(text: str) -> str:
    """macOS keeps file names decomposed (NFD: "й" is "и" + a combining breve), Drive composed (NFC): the
    same name compares unequal unless both are normalized."""
    return unicodedata.normalize("NFC", text)


_FILE_FIELDS = "id, name, mimeType, version, modifiedTime, size, appProperties, parents"


def _parse_time(value: str | None) -> datetime | None:
    return datetime.fromisoformat(value.replace("Z", "+00:00")) if value else None


class DriveClient(Protocol):
    """Blocking calls, like the Google client library: run them with asyncio.to_thread."""

    def upload(self, folder_id: str, name: str, data: bytes, content_type: str, key: str) -> str:
        """Store a file and return its id. `key` makes this idempotent: the same key gives the same file."""
        ...

    def download(self, file_id: str) -> bytes: ...

    def folder_accessible(self, folder_id: str) -> bool: ...

    def list_folder(self, folder_id: str) -> list[DriveEntry]:
        """Direct children of a folder. A Google Doc is listed as a .docx and downloads as one."""
        ...

    def list_children(self, folder_ids: list[str]) -> list[DriveEntry]:
        """Direct children of several folders at once, each with its parent_id (fewer requests)."""
        ...

    def file_info(self, file_id: str) -> DriveEntry:
        """One file's current name, version and size."""
        ...

    def create_folder(self, parent_id: str, name: str) -> str:
        """Create a subfolder and return its id (drive-setup)."""
        ...


class StubDrive:
    def _root(self) -> Path:
        return get_settings().drive_stub_root.resolve()

    def _path(self, relative: str) -> Path:
        path = (self._root() / relative).resolve()
        if not path.is_relative_to(self._root()):
            raise ValueError("path escapes the Drive stub root")
        return path

    def upload(self, folder_id: str, name: str, data: bytes, content_type: str, key: str) -> str:
        file_id = f"{folder_id}/{key}_{name}"
        target = self._path(file_id)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
        return file_id

    def download(self, file_id: str) -> bytes:
        return self._path(file_id).read_bytes()

    def create_folder(self, parent_id: str, name: str) -> str:
        if "/" in name or name in ("", ".", ".."):
            raise ValueError(f"not a folder name: {name!r}")
        folder_id = f"{parent_id}/{name}"
        self._path(folder_id).mkdir(parents=True, exist_ok=True)
        return folder_id

    def folder_accessible(self, folder_id: str) -> bool:
        return _FOLDER_ID.fullmatch(folder_id) is not None

    def _entry(self, child: Path, parent_id: str | None = None) -> DriveEntry:
        stat = child.stat()
        return DriveEntry(
            id=child.relative_to(self._root()).as_posix(),
            name=nfc(child.name),
            is_folder=child.is_dir(),
            content_type=None if child.is_dir() else _BY_SUFFIX.get(child.suffix.lower()),
            version=None if child.is_dir() else str(stat.st_mtime_ns),
            modified_at=datetime.fromtimestamp(stat.st_mtime, UTC),
            size=None if child.is_dir() else stat.st_size,
            parent_id=parent_id,
        )

    def list_folder(self, folder_id: str) -> list[DriveEntry]:
        folder = self._path(folder_id)
        if not folder.is_dir():
            raise FileNotFoundError(f"no folder {folder_id!r} under {self._root()}")
        # Skip Finder files and Word lock files (~$name.docx) from a downloaded copy.
        return [
            self._entry(c, folder_id) for c in sorted(folder.iterdir()) if not c.name.startswith((".", "~$"))
        ]

    def list_children(self, folder_ids: list[str]) -> list[DriveEntry]:
        return [entry for folder_id in folder_ids for entry in self.list_folder(folder_id)]

    def file_info(self, file_id: str) -> DriveEntry:
        path = self._path(file_id)
        if not path.is_file():
            raise FileNotFoundError(f"no file {file_id!r} under {self._root()}")
        return self._entry(path)


_SCOPE = "https://www.googleapis.com/auth/{}"


def load_credentials(path: Path, *, read_only: bool, impersonate: str = ""):
    """Google credentials from GOOGLE_CREDENTIALS_FILE: a service account key, or an OAuth authorized-user
    file with a refresh token. `impersonate` (service account only) uses domain-wide delegation."""
    from google.oauth2 import credentials, service_account

    if not path.is_file():
        raise RuntimeError(
            f"GOOGLE_CREDENTIALS_FILE {path} does not exist (inside Docker it is under /secrets)"
        )
    try:
        info = json.loads(path.read_text())
    except ValueError as exc:
        raise RuntimeError(f"GOOGLE_CREDENTIALS_FILE {path} is not JSON: {exc}") from exc
    scopes = [_SCOPE.format("drive.readonly" if read_only else "drive")]
    if info.get("type") == "service_account":
        creds = service_account.Credentials.from_service_account_info(info, scopes=scopes)
        return creds.with_subject(impersonate) if impersonate else creds
    if impersonate:
        raise RuntimeError("GOOGLE_IMPERSONATE_USER only works with a service account key")
    if info.get("type") != "authorized_user":
        raise RuntimeError(
            f"GOOGLE_CREDENTIALS_FILE has type {info.get('type')!r}: expected a service account key "
            "('service_account') or an authorized-user file ('authorized_user')"
        )
    return credentials.Credentials.from_authorized_user_info(info, scopes=scopes)


def share_hint(account: str | None) -> str:
    """What to do about a folder the app cannot open: almost always, it was never shared with the app."""
    whom = account or "the app's Google service account"
    return (
        f"The app can't open this Drive folder. Share it with {whom} (Viewer is enough), "
        "or check that the link is right."
    )


def explain_error(exc: Exception, account: str | None = None) -> str:
    """A Drive failure in words someone can act on; stored on the version and shown on the Drive screen.

    `account` is the email the app reads Drive as; given, a folder that was never shared names it."""
    from googleapiclient.errors import HttpError

    if not isinstance(exc, HttpError):
        return str(exc)[:500]
    reason = ""
    try:
        details = json.loads(exc.content or b"{}").get("error", {})
        reason = (details.get("errors") or [{}])[0].get("reason", "") or details.get("status", "")
    except (ValueError, AttributeError):
        pass
    status = exc.resp.status
    if reason == "storageQuotaExceeded":
        return (
            "The service account has no Drive storage of its own: put the folder in a Shared Drive or set "
            "GOOGLE_IMPERSONATE_USER (see GOOGLE_DRIVE_SETUP.md)"
        )
    if reason == "accessNotConfigured" or "has not been used in project" in str(exc):
        return "The Google Drive API is not enabled in the service account's Google Cloud project"
    if status == 404:
        return share_hint(account)
    if status == 403 and reason in ("insufficientFilePermissions", "forbidden"):
        return "No permission to add files to this Drive folder: give the account Editor or Content manager"
    if status == 401 or "unauthorized_client" in str(exc):
        return (
            "Google refused the credentials (for GOOGLE_IMPERSONATE_USER: domain-wide delegation not granted)"
        )
    return f"Drive error {status} {reason}: {exc}"[:500]


class GoogleDrive:
    """Drive API v3 with the credentials in GOOGLE_CREDENTIALS_FILE (setup: GOOGLE_DRIVE_SETUP.md).

    The file is either a service account key or an OAuth authorized-user file with a refresh token. A service
    account has no storage quota of its own: it reads folders shared with its email, but uploads only into a
    Shared Drive, unless it acts as a Workspace user (GOOGLE_IMPERSONATE_USER, domain-wide delegation). With
    DRIVE_READ_ONLY=true the read-only scope is requested, so the app cannot change anything in Drive.
    """

    def __init__(self) -> None:
        self._service = None

    def _api(self):
        if self._service is None:
            from googleapiclient.discovery import build

            config = drive_config.current()
            if not config.credentials_file:
                raise RuntimeError(
                    "Google Drive has no credentials: upload a key or set GOOGLE_CREDENTIALS_FILE"
                )
            creds = load_credentials(
                Path(config.credentials_file),
                read_only=config.read_only,
                impersonate=config.impersonate_user,
            )
            self._service = build("drive", "v3", credentials=creds, cache_discovery=False)
        return self._service

    def folder_info(self, folder_id: str) -> dict:
        """Name, Shared Drive (driveId, absent in My Drive) and whether files can be added; for drive-check."""
        return (
            self._api()
            .files()
            .get(
                fileId=self._checked(folder_id),
                fields="id, name, mimeType, driveId, capabilities(canAddChildren)",
                supportsAllDrives=True,
            )
            .execute()
        )

    def create_folder(self, parent_id: str, name: str) -> str:
        if drive_config.current().read_only:
            raise PermissionError("Drive is read-only (DRIVE_READ_ONLY or the setting on Schools & Drive)")
        body = {"name": name, "mimeType": _GOOGLE_FOLDER, "parents": [self._checked(parent_id)]}
        return self._api().files().create(body=body, fields="id", supportsAllDrives=True).execute()["id"]

    def delete(self, file_id: str) -> None:
        self._api().files().delete(fileId=self._checked(file_id), supportsAllDrives=True).execute()

    @staticmethod
    def _checked(drive_id: str) -> str:
        # Ids go into query strings, so only Drive's own id alphabet is allowed.
        if not _DRIVE_ID.fullmatch(drive_id):
            raise ValueError(f"not a Drive id: {drive_id!r}")
        return drive_id

    def upload(self, folder_id: str, name: str, data: bytes, content_type: str, key: str) -> str:
        from googleapiclient.http import MediaIoBaseUpload

        if drive_config.current().read_only:
            raise PermissionError("Drive is read-only (DRIVE_READ_ONLY or the setting on Schools & Drive)")
        files = self._api().files()
        query = (
            f"'{self._checked(folder_id)}' in parents and trashed = false and "
            f"appProperties has {{ key='syllabus_version' and value='{self._checked(key)}' }}"
        )
        found = files.list(
            q=query, fields="files(id)", supportsAllDrives=True, includeItemsFromAllDrives=True
        ).execute()["files"]
        if found:
            return found[0]["id"]
        media = MediaIoBaseUpload(io.BytesIO(data), mimetype=content_type, resumable=True)
        body = {"name": name, "parents": [folder_id], "appProperties": {"syllabus_version": key}}
        return files.create(body=body, media_body=media, fields="id", supportsAllDrives=True).execute()["id"]

    def download(self, file_id: str) -> bytes:
        from googleapiclient.http import MediaIoBaseDownload

        files = self._api().files()
        mime = files.get(fileId=self._checked(file_id), fields="mimeType", supportsAllDrives=True).execute()
        if mime["mimeType"] == _GOOGLE_DOC:
            request = files.export_media(fileId=file_id, mimeType=DOCX)
        else:
            request = files.get_media(fileId=file_id, supportsAllDrives=True)
        buffer = io.BytesIO()
        downloader = MediaIoBaseDownload(buffer, request)
        done = False
        while not done:
            _, done = downloader.next_chunk()
        return buffer.getvalue()

    def folder_accessible(self, folder_id: str) -> bool:
        from googleapiclient.errors import HttpError

        if not _DRIVE_ID.fullmatch(folder_id):
            return False
        try:
            found = (
                self._api().files().get(fileId=folder_id, fields="mimeType", supportsAllDrives=True).execute()
            )
        except HttpError as exc:
            if exc.resp.status in (403, 404):
                return False
            raise
        return found["mimeType"] == _GOOGLE_FOLDER

    @staticmethod
    def _entry(f: dict) -> DriveEntry:
        parents = f.get("parents") or [None]
        common = {
            "version": f.get("version"),
            "modified_at": _parse_time(f.get("modifiedTime")),
            "app_version_id": (f.get("appProperties") or {}).get("syllabus_version"),
            "parent_id": parents[0],
        }
        if f["mimeType"] == _GOOGLE_FOLDER:
            return DriveEntry(f["id"], nfc(f["name"]), is_folder=True, parent_id=parents[0])
        if f["mimeType"] == _GOOGLE_DOC:
            return DriveEntry(f["id"], nfc(f"{f['name']}.docx"), is_folder=False, content_type=DOCX, **common)
        suffix_type = _BY_SUFFIX.get(Path(f["name"]).suffix.lower())
        size = int(f["size"]) if f.get("size") else None
        return DriveEntry(
            f["id"], nfc(f["name"]), is_folder=False, content_type=suffix_type, size=size, **common
        )

    def _list(self, query: str) -> list[DriveEntry]:
        files = self._api().files()
        entries: list[DriveEntry] = []
        token = None
        while True:
            page = files.list(
                q=query,
                fields=f"nextPageToken, files({_FILE_FIELDS})",
                pageSize=1000,
                pageToken=token,
                orderBy="name",
                supportsAllDrives=True,
                includeItemsFromAllDrives=True,
            ).execute()
            entries += [self._entry(f) for f in page["files"]]
            token = page.get("nextPageToken")
            if not token:
                return entries

    def list_folder(self, folder_id: str) -> list[DriveEntry]:
        return self._list(f"'{self._checked(folder_id)}' in parents and trashed = false")

    def list_children(self, folder_ids: list[str]) -> list[DriveEntry]:
        entries: list[DriveEntry] = []
        # Drive limits the query length; 40 parents per request stay well under it.
        for i in range(0, len(folder_ids), 40):
            parents = " or ".join(f"'{self._checked(f)}' in parents" for f in folder_ids[i : i + 40])
            entries += self._list(f"({parents}) and trashed = false")
        # A query over several parents leaves out files the account reaches only through a shared link (the
        # School shared its folder by link, not with the account), while asking one folder at a time finds
        # them. So every folder that came back empty is asked on its own; empty ones really are empty.
        # Asked side by side, each worker with its own client: the Drive client is not thread-safe.
        seen = {e.parent_id for e in entries}
        alone = [f for f in folder_ids if f not in seen]
        if alone:
            import threading
            from concurrent.futures import ThreadPoolExecutor

            # One client per worker thread, reused for every folder that thread asks about: building a client
            # signs in to Google, which costs more than the listing itself.
            local = threading.local()

            def listed(folder_id: str) -> list[DriveEntry]:
                if not hasattr(local, "client"):
                    local.client = GoogleDrive()
                return local.client.list_folder(folder_id)

            with ThreadPoolExecutor(max_workers=8) as pool:
                for found in pool.map(listed, alone):
                    entries += found
        return entries

    def file_info(self, file_id: str) -> DriveEntry:
        found = (
            self._api()
            .files()
            .get(fileId=self._checked(file_id), fields=_FILE_FIELDS + ", trashed", supportsAllDrives=True)
            .execute()
        )
        if found.get("trashed"):
            raise FileNotFoundError(f"Drive file {file_id} is in the trash")
        return self._entry(found)


def get_drive() -> DriveClient:
    if drive_config.current().provider == "stub":
        return StubDrive()
    return GoogleDrive()
