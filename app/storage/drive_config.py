"""Which Google Drive the app uses: the settings saved in the app (Schools & Drive screen, `/drive` API) or,
when nothing was saved there, the DRIVE_* / GOOGLE_* variables from .env.

The saved settings live in MongoDB (`drive_settings`, one document) and the uploaded key in a file under
SECRETS_ROOT, readable only by the app's user. Every process (API, workers, CLI) reads them with `refresh()`
before it talks to Drive, so a change made in the UI reaches the workers without a restart.
"""

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from app.config import get_settings

log = logging.getLogger(__name__)

KEY_FILENAME = "google-drive.json"


@dataclass(frozen=True)
class DriveConfig:
    provider: Literal["stub", "google"]
    credentials_file: Path | None
    impersonate_user: str
    read_only: bool
    # Versions whose School has no folder go here: the shared folder when one is set, else DRIVE_DEFAULT_FOLDER_ID.
    default_folder_id: str
    shared_folder_id: str
    # app: the uploaded key is used; env: .env decides the provider and credentials.
    source: Literal["env", "app"]


def key_path() -> Path:
    return get_settings().secrets_root / KEY_FILENAME


def from_env() -> DriveConfig:
    s = get_settings()
    return DriveConfig(
        provider=s.drive_provider,
        credentials_file=s.google_credentials_file,
        impersonate_user=s.google_impersonate_user,
        read_only=s.drive_read_only,
        default_folder_id=s.drive_default_folder_id,
        shared_folder_id="",
        source="env",
    )


# Saved settings as last read by this process; None = nothing saved in the app.
_saved: DriveConfig | None = None


def current() -> DriveConfig:
    return _saved or from_env()


def combine(doc) -> DriveConfig | None:
    """The effective config for a saved `DriveSettingsDoc` (None when there is none)."""
    if doc is None:
        return None
    env = from_env()
    use_key = doc.credentials_saved
    return DriveConfig(
        provider="google" if use_key else env.provider,
        credentials_file=key_path() if use_key else env.credentials_file,
        impersonate_user=doc.impersonate_user if use_key else env.impersonate_user,
        read_only=doc.read_only,
        default_folder_id=doc.shared_folder_id or env.default_folder_id,
        shared_folder_id=doc.shared_folder_id,
        source="app" if use_key else "env",
    )


async def refresh() -> DriveConfig:
    """Read the saved settings again. If MongoDB cannot be read, the last known config is kept."""
    global _saved
    from app.documents import DriveSettingsDoc

    try:
        doc = await DriveSettingsDoc.find_one()
    except Exception:
        log.exception("could not read the Drive settings, keeping the last known ones")
        return current()
    _saved = combine(doc)
    return current()


def forget() -> None:
    """Drop the cached settings (tests)."""
    global _saved
    _saved = None
