"""Point the app at one shared Drive folder (`python -m app.cli drive-setup <folder id>`).

The shared folder holds one subfolder per School. For every School in
the database this finds its subfolder by name, creates it when missing (unless DRIVE_READ_ONLY or --no-create),
and stores its id as the School's upload folder for the semester. That is what the "Schools & Drive" screen
does by hand, one folder at a time. New versions are then copied there by `sync_drive_files`.
"""

import asyncio
from dataclasses import dataclass, field

import sqlalchemy as sa

from app.db.pg import session_scope
from app.models import School, UploadFolder
from app.storage import drive_config
from app.storage.drive import get_drive


@dataclass
class SchoolLink:
    school: str
    # found: the subfolder exists; created: made now; would create: --dry-run; missing: not made (read-only)
    action: str
    drive_folder_id: str | None = None
    upload_folders: int = 0


@dataclass
class SetupReport:
    links: list[SchoolLink] = field(default_factory=list)
    folders_created_in_db: int = 0
    folders_updated_in_db: int = 0


def _key(name: str) -> str:
    return " ".join(name.split()).casefold()


async def setup_shared_folder(
    root_id: str, semester: str, *, create: bool = True, dry_run: bool = False
) -> SetupReport:
    config = await drive_config.refresh()
    drive = get_drive()
    root_id = root_id.strip()
    entries = await asyncio.to_thread(drive.list_folder, root_id)
    subfolders = {_key(e.name): e for e in entries if e.is_folder}
    can_create = create and not dry_run and not config.read_only
    report = SetupReport()

    async with session_scope() as session:
        for school in await session.scalars(sa.select(School).order_by(School.name)):
            entry = subfolders.get(_key(school.name))
            if entry is not None:
                link = SchoolLink(school.name, "found", entry.id)
            elif can_create:
                created = await asyncio.to_thread(drive.create_folder, root_id, school.name)
                link = SchoolLink(school.name, "created", created)
            else:
                link = SchoolLink(school.name, "would create" if dry_run and create else "missing")
            report.links.append(link)
            if link.drive_folder_id is None or dry_run:
                continue

            folders = list(
                await session.scalars(
                    sa.select(UploadFolder).where(
                        UploadFolder.school_id == school.id, UploadFolder.semester == semester
                    )
                )
            )
            for folder in folders:
                if folder.drive_folder_id != link.drive_folder_id:
                    folder.drive_folder_id = link.drive_folder_id
                    report.folders_updated_in_db += 1
            if not folders:
                path = school.name[:300]
                taken = await session.scalar(
                    sa.select(UploadFolder.id).where(
                        UploadFolder.school_id == school.id, UploadFolder.path == path
                    )
                )
                if taken:  # the School's folder of another semester; paths are unique per School
                    path = f"{school.name} · {semester}"[:300]
                session.add(
                    UploadFolder(
                        school_id=school.id,
                        path=path,
                        semester=semester,
                        drive_folder_id=link.drive_folder_id,
                    )
                )
                report.folders_created_in_db += 1
            link.upload_folders = max(len(folders), 1)
        if dry_run:
            await session.rollback()
    return report
