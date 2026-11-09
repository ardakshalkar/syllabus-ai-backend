"""Syllabus files. An uploaded version lives on local disk, FILES_ROOT/{syllabus_id}/v{n}/{filename} (paths in
the DB are relative), and Drive keeps its durable copy: a file missing here is restored from it.

A version read from Google Drive (services/drive_catalog.py) has no local file: `local_copy` downloads it to
TMP_ROOT for as long as it is needed and deletes it afterwards, so the app never keeps it.
"""

import asyncio
import contextlib
import hashlib
import re
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from app.config import get_settings
from app.storage import drive, drive_config

if TYPE_CHECKING:
    from app.models import SyllabusVersion

_UNSAFE = re.compile(r"[^\w.\- ]+", re.UNICODE)


@dataclass
class StoredFile:
    relative_path: str
    size: int
    sha256: str


def safe_filename(name: str) -> str:
    base = Path(name).name.strip() or "syllabus"
    cleaned = _UNSAFE.sub("_", base)[:150]
    return cleaned or "syllabus"


def root() -> Path:
    return get_settings().files_root.resolve()


def _write(target: Path, data: bytes) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_name(f".{uuid.uuid4().hex}.part")
    tmp.write_bytes(data)
    tmp.replace(target)


def save_version_file(syllabus_id: uuid.UUID, version: int, filename: str, data: bytes) -> StoredFile:
    relative = Path(str(syllabus_id)) / f"v{version}" / safe_filename(filename)
    _write(root() / relative, data)
    return StoredFile(str(relative), len(data), hashlib.sha256(data).hexdigest())


def absolute(relative_path: str) -> Path:
    path = (root() / relative_path).resolve()
    if not path.is_relative_to(root()):
        raise ValueError("path escapes the files root")
    return path


async def ensure_local(version: "SyllabusVersion") -> Path:
    """An uploaded version's file on disk. Raises FileNotFoundError if it is gone and never reached Drive.
    For a version read from Drive use `local_copy`."""
    if version.file_path is None:
        raise FileNotFoundError(f"version {version.id} lives in Google Drive only")
    path = absolute(version.file_path)
    if await asyncio.to_thread(path.exists):
        return path
    if not version.drive_file_id:
        raise FileNotFoundError(version.file_path)
    await drive_config.refresh()
    data = await asyncio.to_thread(drive.get_drive().download, version.drive_file_id)
    if hashlib.sha256(data).hexdigest() != version.sha256:
        raise ValueError(f"Drive returned a different file for version {version.id}")
    await asyncio.to_thread(_write, path, data)
    return path


@contextlib.asynccontextmanager
async def local_copy(version: "SyllabusVersion") -> AsyncIterator[Path]:
    """The version's file on disk while the block runs: the stored file, or for a version read from Drive a
    temporary download that is deleted afterwards."""
    if version.file_path is not None:
        yield await ensure_local(version)
        return
    if not version.drive_file_id:
        raise FileNotFoundError(f"version {version.id} has no file")
    data = await read_version(version)
    suffix = Path(version.filename).suffix.lower() or ".docx"
    path = tmp_dir() / f"drive-{uuid.uuid4().hex}{suffix}"
    await asyncio.to_thread(path.write_bytes, data)
    try:
        yield path
    finally:
        await asyncio.to_thread(path.unlink, missing_ok=True)


async def read_version(version: "SyllabusVersion") -> bytes:
    """The version's bytes: from local disk, or straight from Google Drive for a version read from it."""
    if version.file_path is not None:
        return await asyncio.to_thread((await ensure_local(version)).read_bytes)
    if not version.drive_file_id:
        raise FileNotFoundError(f"version {version.id} has no file")
    await drive_config.refresh()
    return await asyncio.to_thread(drive.get_drive().download, version.drive_file_id)


def tmp_dir() -> Path:
    path = get_settings().tmp_root.resolve()
    path.mkdir(parents=True, exist_ok=True)
    return path
