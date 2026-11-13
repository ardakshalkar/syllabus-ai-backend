"""Syllabi read from Google Drive, without importing them.

Each School has a Drive folder per semester (upload_folders.drive_folder_id, set on Schools & Drive). Inside it
are discipline folders, and in them the syllabus files (.docx, .pdf, Google Docs), possibly in subfolders:

    Осень 2026-2027 силлабусы/            ← the School's folder for the semester
        Алгоритмизация и программирование/
            Силлабус_Алгоритмизация.docx  ← one syllabus

`sync_folder` reads that tree's metadata only (id, name, Drive's revision number) and keeps a catalog of it:
a course "<discipline> · <file name>" per file, its syllabus for the semester, and one version per Drive
revision. The files themselves are never stored: a check reads the file from Drive when it runs
(`files.local_copy`) and `follow_drive` first moves the run to the file's newest revision. A file that left
the folder is marked drive_missing_at (results kept, hidden from lists); files this app itself copied into
Drive (drive_sync, tagged with appProperties) are not catalogued twice.

Sync runs from Schools & Drive ("Refresh from Drive") and from celery beat every DRIVE_SYNC_INTERVAL_S.
"""

import asyncio
import hashlib
import json
import logging
import re
import uuid
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.pg import session_scope
from app.enums import AiStatus, ApprovalStage, VersionSource
from app.models import (
    ApprovalEvent,
    AssessmentRun,
    Course,
    Program,
    School,
    SessionItem,
    Syllabus,
    SyllabusVersion,
    UploadFolder,
)
from app.storage import cache, drive_config
from app.storage.drive import DriveClient, DriveEntry, explain_error, get_drive, nfc

log = logging.getLogger(__name__)

_LOCK = "drive-catalog:lock"
_LOCK_TTL_S = 15 * 60
# Set when a sync is asked for, until a worker takes it, so the UI shows it running from the first moment.
_REQUESTED = "drive-catalog:requested"
_REQUESTED_TTL_S = 120
_STATUS_TTL_S = 30 * 24 * 3600
UNKNOWN_LANGUAGE = "Unknown"
# Folders a School keeps beside its course folders for reference, not syllabi: study plans and programme
# passports (SDT's folder holds 426 study-plan files). Matched on the top-level folder's name, any language.
REFERENCE_FOLDER = re.compile(
    r"учебн\w*\s+план|оқу\s+жосп|паспорт|study\s+plan|curricul|passport", re.IGNORECASE
)


def _status_key(folder_id: uuid.UUID) -> str:
    return f"drive-catalog:folder:{folder_id}"


def course_slug(drive_file_id: str) -> str:
    """Stable and ASCII whatever the file is called, so the next sync finds the same course."""
    return f"drive-{hashlib.sha1(drive_file_id.encode()).hexdigest()[:12]}"


def drive_link(drive_file_id: str | None) -> str | None:
    if not drive_file_id or "/" in drive_file_id:  # a local stub path has no web page
        return None
    return f"https://drive.google.com/file/d/{drive_file_id}/view"


@dataclass
class SyncReport:
    folder_id: str
    school: str
    semester: str
    drive_folder_id: str | None
    disciplines: int = 0
    files: int = 0
    added: int = 0
    updated: int = 0
    unchanged: int = 0
    missing: int = 0
    skipped: list[dict[str, str]] = field(default_factory=list)
    error: str | None = None
    synced_at: str = field(default_factory=lambda: datetime.now(UTC).isoformat())


@dataclass(frozen=True)
class FoundFile:
    discipline: str
    path: str
    entry: DriveEntry
    # Drive id of the course folder it sits under, so excluding that folder excludes it.
    course_id: str = ""

    @property
    def course_name(self) -> str:
        return nfc(f"{self.discipline} · {Path(self.entry.name).stem.strip()}")[:200]


# A listing younger than this is reused by a read that asks for it ("Choose files" → "Save and read in"),
# instead of listing the whole folder from Drive a second time.
LISTING_REUSE_S = 300


@dataclass
class Listing:
    """Everything in a School folder as Drive showed it, before the School's exclusions are applied: the
    course folders directly in it (empty ones included), every syllabus file below them, and what was
    skipped. Kept on the folder as JSON, so it can be shown and read again without asking Drive."""

    courses: list[tuple[str, str]]  # (Drive id, name)
    found: list[FoundFile]
    skipped: list[dict[str, str]]

    def to_json(self) -> dict[str, Any]:
        return {
            "courses": [list(c) for c in self.courses],
            "skipped": self.skipped,
            "found": [
                {
                    "discipline": f.discipline,
                    "path": f.path,
                    "course_id": f.course_id,
                    "id": f.entry.id,
                    "name": f.entry.name,
                    "content_type": f.entry.content_type,
                    "version": f.entry.version,
                    "modified_at": f.entry.modified_at.isoformat() if f.entry.modified_at else None,
                    "size": f.entry.size,
                }
                for f in self.found
            ],
        }

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> "Listing":
        return cls(
            courses=[(c[0], c[1]) for c in data.get("courses", [])],
            skipped=list(data.get("skipped", [])),
            found=[
                FoundFile(
                    f["discipline"],
                    f["path"],
                    DriveEntry(
                        f["id"],
                        f["name"],
                        is_folder=False,
                        content_type=f.get("content_type"),
                        version=f.get("version"),
                        modified_at=datetime.fromisoformat(f["modified_at"])
                        if f.get("modified_at")
                        else None,
                        size=f.get("size"),
                    ),
                    f.get("course_id", ""),
                )
                for f in data.get("found", [])
            ],
        )

    def taken(self, excluded: frozenset[str]) -> list[FoundFile]:
        """The files a read takes: everything but what the School excluded, file by file or a course folder."""
        return [f for f in self.found if f.entry.id not in excluded and f.course_id not in excluded]


def list_folder_tree(client: DriveClient, root_id: str) -> Listing:
    """Blocking. Every syllabus file below the course folders of `root_id`, what was skipped and why. Each
    level of subfolders costs one batched request (more for a folder shared only by link, see list_children)."""
    found: list[FoundFile] = []
    skipped: list[dict[str, str]] = []
    courses: list[tuple[str, str]] = []
    discipline: dict[str, str] = {}
    course_of: dict[str, str] = {}
    path: dict[str, str] = {}
    for entry in client.list_folder(root_id):
        name = nfc(entry.name.strip())
        if entry.is_folder and REFERENCE_FOLDER.search(entry.name):
            skipped.append({"path": f"{name}/", "reason": "study plans or passports, not syllabi"})
        elif entry.is_folder:
            discipline[entry.id] = path[entry.id] = name
            course_of[entry.id] = entry.id
            courses.append((entry.id, name))
        elif not entry.app_version_id:
            skipped.append({"path": entry.name, "reason": "not inside a discipline folder"})
    level = list(discipline)
    while level:
        next_level = []
        for child in client.list_children(level):
            parent = child.parent_id or ""
            if parent not in discipline:
                continue
            child_path = f"{path[parent]}/{child.name}"
            if child.is_folder:
                discipline[child.id], path[child.id] = discipline[parent], child_path
                course_of[child.id] = course_of[parent]
                next_level.append(child.id)
            elif child.app_version_id:
                continue  # a version this app copied into Drive: already catalogued as that version
            elif child.content_type:
                found.append(FoundFile(discipline[parent], child_path, child, course_of[parent]))
            else:
                skipped.append({"path": child_path, "reason": "not a .docx, .pdf or Google Doc"})
        level = next_level
    return Listing(sorted(courses, key=lambda c: c[1]), found, skipped)


async def folder_listing(folder_id: uuid.UUID, *, max_age_s: float | None) -> tuple[Listing, datetime]:
    """The folder's listing and when it was taken: the saved one if it is no older than `max_age_s` (None:
    any age), else a new one from Drive, which is then saved."""
    async with session_scope() as session:
        folder = await session.get(UploadFolder, folder_id)
        if folder is None:
            raise LookupError("folder not found")
        if not folder.drive_folder_id:
            raise ValueError("No Google Drive folder is set for this School and semester")
        root, saved, listed_at = folder.drive_folder_id, folder.listing, folder.listed_at
    now = datetime.now(UTC)
    if saved and listed_at and (max_age_s is None or (now - listed_at).total_seconds() <= max_age_s):
        return Listing.from_json(saved), listed_at
    await drive_config.refresh()
    listing = await asyncio.to_thread(list_folder_tree, get_drive(), root)
    async with session_scope() as session:
        folder = await session.get(UploadFolder, folder_id)
        if folder is not None:
            folder.listing, folder.listed_at = listing.to_json(), now
    return listing, now


async def folder_contents(
    session: AsyncSession, folder: UploadFolder, *, refresh: bool = False
) -> dict[str, Any]:
    """What is inside a School folder: each course folder with its files, each file with the syllabus the app
    keeps for it if it has read it, and what the School excluded, marked. From the saved listing unless
    `refresh` (or none was saved yet); what the app made of each file is always current."""
    listing, listed_at = await folder_listing(folder.id, max_age_s=0 if refresh else None)
    excluded = frozenset(folder.excluded_drive_ids or [])
    ids = [f.entry.id for f in listing.found]
    known: dict[str, Syllabus] = {}
    if ids:
        rows = await session.execute(
            sa.select(SyllabusVersion.drive_file_id, Syllabus)
            .join(Syllabus, Syllabus.id == SyllabusVersion.syllabus_id)
            .where(
                SyllabusVersion.drive_file_id.in_(ids),
                Syllabus.semester == folder.semester,
                Syllabus.drive_missing_at.is_(None),
            )
        )
        known = {file_id: syllabus for file_id, syllabus in rows.all() if file_id}
    courses: dict[str, dict[str, Any]] = {
        cid: {"name": name, "drive_id": cid, "excluded": cid in excluded, "files": []}
        for cid, name in listing.courses
    }
    for f in listing.found:
        s = known.get(f.entry.id)
        course = courses.setdefault(
            f.course_id or f.discipline,
            {"name": f.discipline, "drive_id": None, "excluded": False, "files": []},
        )
        course["files"].append(
            {
                "drive_id": f.entry.id,
                "name": f.entry.name,
                "path": f.path,
                "drive_link": drive_link(f.entry.id),
                "modified_at": f.entry.modified_at,
                "excluded": f.entry.id in excluded or f.course_id in excluded,
                "syllabus_id": s.id if s else None,
                "ai_status": s.ai_status if s else None,
                "issues": s.issues if s else None,
            }
        )
    return {
        "folder_id": folder.id,
        "listed_at": listed_at,
        "courses": sorted(courses.values(), key=lambda c: c["name"]),
        "skipped": listing.skipped,
    }


async def set_excluded(
    session: AsyncSession, folder: UploadFolder, drive_id: str, name: str | None, excluded: bool
) -> list[str]:
    """Exclude a file or course folder of the School's folder from being read in, or include it again.
    Syllabi already read in from it are hidden (or shown again) right away, as a file that left the folder
    would be, so the change shows without waiting for the next read."""
    ids = [i for i in (folder.excluded_drive_ids or []) if i != drive_id]
    if excluded:
        ids.append(drive_id)
    folder.excluded_drive_ids = ids
    # The syllabi this file, or this course folder, brought in.
    from_file = sa.select(SyllabusVersion.syllabus_id).where(SyllabusVersion.drive_file_id == drive_id)
    match: sa.ColumnElement[bool] = Syllabus.id.in_(from_file)
    if name:
        in_course = sa.select(Course.id).where(Course.discipline == nfc(name.strip())[:200])
        match = sa.or_(match, Syllabus.course_id.in_(in_course))
    for syllabus in await session.scalars(
        sa.select(Syllabus).where(
            Syllabus.folder_id == folder.id, Syllabus.semester == folder.semester, match
        )
    ):
        syllabus.drive_missing_at = datetime.now(UTC) if excluded else None
    return ids


async def add_drive_version(
    session: AsyncSession, syllabus: Syllabus, entry: DriveEntry, folder_id: uuid.UUID | None
) -> SyllabusVersion:
    """A new version for a new Drive revision of the syllabus's file. Like an upload, the syllabus goes back
    to Draft until this version's check completes."""
    number = syllabus.current_version + 1
    version = SyllabusVersion(
        syllabus_id=syllabus.id,
        version=number,
        filename=entry.name[:255],
        file_path=None,
        content_type=entry.content_type or "application/octet-stream",
        size=entry.size or 0,
        sha256=None,
        created_by=None,
        source=VersionSource.drive,
        folder_id=folder_id,
        drive_file_id=entry.id,
        drive_version=entry.version,
        drive_modified_at=entry.modified_at,
        drive_synced_at=datetime.now(UTC),
    )
    session.add(version)
    previous_stage = syllabus.approval_stage
    syllabus.current_version = number
    syllabus.issues = None
    syllabus.ai_status = AiStatus.not_checked
    syllabus.approval_stage = ApprovalStage.draft
    syllabus.drive_missing_at = None
    session.add(
        ApprovalEvent(
            syllabus_id=syllabus.id,
            kind="version",
            version=number,
            from_stage=previous_stage,
            to_stage=ApprovalStage.draft,
            actor_id=None,
            comment=f"Read v{number} from Google Drive ({version.filename})",
        )
    )
    await session.flush()
    return version


async def _latest_drive_version(
    session: AsyncSession, syllabus_id: uuid.UUID, drive_file_id: str
) -> SyllabusVersion | None:
    return await session.scalar(
        sa.select(SyllabusVersion)
        .where(SyllabusVersion.syllabus_id == syllabus_id, SyllabusVersion.drive_file_id == drive_file_id)
        .order_by(SyllabusVersion.version.desc())
        .limit(1)
    )


async def _apply(
    session: AsyncSession,
    folder: UploadFolder,
    program_id: uuid.UUID,
    found: list[FoundFile],
    report: SyncReport,
) -> None:
    by_slug = {course_slug(f.entry.id): f for f in found}
    courses = {
        c.slug: c for c in await session.scalars(sa.select(Course).where(Course.slug.in_(list(by_slug))))
    }
    for slug, f in by_slug.items():
        course = courses.get(slug)
        if course is None:
            course = Course(
                slug=slug,
                name=f.course_name,
                program_id=program_id,
                credits=0,
                lead_id=None,
                discipline=f.discipline[:200],
            )
            session.add(course)
            await session.flush()
        elif course.name != f.course_name or course.discipline != f.discipline[:200]:
            course.name, course.discipline = f.course_name, f.discipline[:200]  # renamed in Drive
        syllabus = await session.scalar(
            sa.select(Syllabus).where(Syllabus.course_id == course.id, Syllabus.semester == folder.semester)
        )
        if syllabus is None:
            syllabus = Syllabus(
                course_id=course.id,
                professor_id=None,
                language=UNKNOWN_LANGUAGE,
                semester=folder.semester,
                current_version=0,
                ai_status=AiStatus.not_checked,
                approval_stage=ApprovalStage.draft,
                folder_id=folder.id,
            )
            session.add(syllabus)
            await session.flush()
        syllabus.drive_missing_at = None
        latest = await _latest_drive_version(session, syllabus.id, f.entry.id)
        if latest is not None and latest.drive_version == f.entry.version:
            report.unchanged += 1
            continue
        await add_drive_version(session, syllabus, f.entry, folder.id)
        if latest is None:
            report.added += 1
        else:
            report.updated += 1

    # Files that left the folder: their syllabi stay with their results, hidden from lists.
    gone = await session.scalars(
        sa.select(Syllabus)
        .join(Course, Course.id == Syllabus.course_id)
        .where(
            Syllabus.folder_id == folder.id,
            Syllabus.drive_missing_at.is_(None),
            Course.slug.like("drive-%"),
            Course.slug.not_in(list(by_slug) or [""]),
        )
    )
    now = datetime.now(UTC)
    for syllabus in gone:
        syllabus.drive_missing_at = now
        report.missing += 1


async def sync_folder(folder_id: uuid.UUID, *, reuse_listing_s: float = 0) -> SyncReport:
    """Bring the catalog of one School folder up to date with Drive. Errors are reported, not raised.

    `reuse_listing_s`: a saved listing at most this old is used instead of listing Drive again."""
    await drive_config.refresh()
    async with session_scope() as session:
        folder = await session.get(UploadFolder, folder_id)
        if folder is None:
            raise LookupError("folder not found")
        school = await session.get(School, folder.school_id)
        assert school is not None
        program_id = await session.scalar(
            sa.select(Program.id).where(Program.school_id == school.id).order_by(Program.name).limit(1)
        )
        report = SyncReport(str(folder.id), school.name, folder.semester, folder.drive_folder_id)
        root = folder.drive_folder_id
        excluded = list(folder.excluded_drive_ids or [])
    if not root:
        report.error = "No Google Drive folder is set for this School and semester"
    elif program_id is None:
        report.error = "This School has no Program (run `python -m app.cli setup-schools`)"
    else:
        try:
            listing, _ = await folder_listing(folder_id, max_age_s=reuse_listing_s)
            out = frozenset(excluded)
            found = listing.taken(out)
            report.disciplines = sum(1 for cid, _ in listing.courses if cid not in out)
            report.files, report.skipped = len(found), listing.skipped
            async with session_scope() as session:
                folder = await session.get(UploadFolder, folder_id)
                assert folder is not None
                await _apply(session, folder, program_id, found, report)
        except Exception as exc:
            log.exception("Drive sync of folder %s failed", folder_id)
            from app.services import drive_admin

            report.error = explain_error(exc, (await drive_admin.status()).get("client_email"))
    async with session_scope() as session:
        folder = await session.get(UploadFolder, folder_id)
        if folder is not None:
            folder.read_at, folder.read_error = datetime.now(UTC), report.error
    try:
        await cache.put_json(_status_key(folder_id), asdict(report), _STATUS_TTL_S)
    except Exception:
        log.warning("could not keep the Drive sync report of folder %s", folder_id, exc_info=True)
    log.info(
        "Drive sync %s %s: %s files, %s added, %s updated, %s missing%s",
        report.school,
        report.semester,
        report.files,
        report.added,
        report.updated,
        report.missing,
        f", error: {report.error}" if report.error else "",
    )
    return report


async def sync_all(school_id: uuid.UUID | None = None) -> list[SyncReport]:
    """Sync every School folder that has a Drive folder (or only one School's). One sync at a time."""
    locked = await cache.client().set(_LOCK, "1", nx=True, ex=_LOCK_TTL_S)
    await cache.client().delete(_REQUESTED)
    if not locked:
        log.info("a Drive sync is already running")
        return []
    try:
        async with session_scope() as session:
            query = sa.select(UploadFolder.id).where(UploadFolder.drive_folder_id.is_not(None))
            if school_id is not None:
                query = query.where(UploadFolder.school_id == school_id)
            folder_ids = list(await session.scalars(query))
        return [await sync_folder(f) for f in folder_ids]
    finally:
        await cache.client().delete(_LOCK)


async def mark_requested() -> None:
    await cache.client().set(_REQUESTED, "1", ex=_REQUESTED_TTL_S)


async def sync_running() -> bool:
    """A sync is running, or was asked for and waits for a worker."""
    return bool(await cache.client().exists(_LOCK, _REQUESTED))


async def scheduled_sync() -> int:
    """celery beat: keep the catalog fresh, with real Google Drive only."""
    if (await drive_config.refresh()).provider != "google":
        return 0
    return len(await sync_all())


async def last_reports(folder_ids: list[uuid.UUID]) -> dict[uuid.UUID, dict[str, Any]]:
    if not folder_ids:
        return {}
    values = await cache.client().mget([_status_key(f) for f in folder_ids])
    return {f: json.loads(v) for f, v in zip(folder_ids, values, strict=True) if v}


class DriveFileGone(Exception):
    """The syllabus's file is no longer in Google Drive."""


async def follow_drive(run_id: uuid.UUID, version: SyllabusVersion) -> SyllabusVersion:
    """Before checking a version read from Drive: if the file changed in Drive since, add the new revision as
    a version and move the run (and its session item) to it, so a check always reads what Drive holds now."""
    if version.source != VersionSource.drive or not version.drive_file_id:
        return version
    await drive_config.refresh()
    try:
        entry = await asyncio.to_thread(get_drive().file_info, version.drive_file_id)
    except FileNotFoundError as exc:
        raise DriveFileGone(str(exc)) from exc
    except Exception as exc:
        from googleapiclient.errors import HttpError

        if isinstance(exc, HttpError) and exc.resp.status == 404:
            raise DriveFileGone(explain_error(exc)) from exc
        raise
    async with session_scope() as session:
        syllabus = await session.scalar(
            sa.select(Syllabus).where(Syllabus.id == version.syllabus_id).with_for_update()
        )
        assert syllabus is not None
        latest = await _latest_drive_version(session, syllabus.id, version.drive_file_id)
        if latest is not None and latest.drive_version == entry.version:
            current = latest
        else:
            current = await add_drive_version(session, syllabus, entry, version.folder_id)
        if current.id != version.id:
            await session.execute(
                sa.update(AssessmentRun).where(AssessmentRun.id == run_id).values(version_id=current.id)
            )
            await session.execute(
                sa.update(SessionItem).where(SessionItem.run_id == run_id).values(version_id=current.id)
            )
        session.expunge(current)
        return current
