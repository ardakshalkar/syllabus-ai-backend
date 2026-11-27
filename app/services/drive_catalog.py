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
from enum import Enum, auto
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
from app.services.syllabi import external_models
from app.storage import cache, drive_config
from app.storage.drive import DriveClient, DriveEntry, explain_error, explain_folder_error, get_drive, nfc

log = logging.getLogger(__name__)

_LOCK = "drive-catalog:lock"
_LOCK_TTL_S = 15 * 60
# Set when a sync is asked for, until a worker takes it, so the UI shows it running from the first moment.
_REQUESTED = "drive-catalog:requested"
_REQUESTED_TTL_S = 120
_STATUS_TTL_S = 30 * 24 * 3600
UNKNOWN_LANGUAGE = "Unknown"

# Folders a School keeps beside or inside its course folders for reference, archives, guidelines or templates, not syllabi.
REFERENCE_FOLDER = re.compile(
    r"учебн\w*\s+план|оқу\s+жосп|паспорт|study\s+plan|curricul|passport|"
    r"архив|archive|\bold\b|ескі|бұрынғы|"
    r"шаблон|үлгі|template|форма\b|"
    r"инструкци|нұсқау|guideline|методич|положен|правил|"
    r"расписани|кесте\w*|schedule|график|"
    r"кэд|каталог\s+электив|рубрикатор",
    re.IGNORECASE,
)

# Files that are clearly not syllabi: Office lock files, readmes, templates, instructions, schedules, etc.
NON_SYLLABUS_FILE = re.compile(
    r"^~\$|^\.|"
    r"^(readme|read\s*me|описание|нұсқаулық)($|[\s_\-–—.:])|"
    r"^(шаблон\w*|үлгі\w*|template\w*)($|[\s_\-–—.:])|"
    r"^(инструкци\w*|guideline\w*|методич\w*|положен\w*|правил\w*)($|[\s_\-–—.:])|"
    r"^(расписани\w*|график\w*|кесте\w*|schedule\w*)($|[\s_\-–—.:])|"
    r"^(ведомост\w*|журнал\w*|тізім\w*|список\w*)($|[\s_\-–—.:])|"
    r"^(учебн\w*\s+план|оқу\s+жосп\w*|паспорт\s+оп|study\s+plan)($|[\s_\-–—.:])",
    re.IGNORECASE,
)

_PROGRAM_FOLDER = re.compile(r"^\s*(г?оп|г?ббб?|ббт|ep|op|gep)\s*[.:\-–—]?\s+(.+?)\s*$", re.IGNORECASE)
_CODE_FOLDER = re.compile(r"^[678]\s?[BMDВМДbmdвмд]\s?\d{5}", re.IGNORECASE)

_DEGREE_PATTERN = re.compile(
    r"^\s*(бакалавр\w*|магистр\w*|докторант\w*|phd\b|bachelor\w*|master\w*|нпм\b|пм\b)\s*$",
    re.IGNORECASE,
)

_YEAR_PATTERN = re.compile(
    r"^\s*([1-4]\s*[-_]?\s*(курс|год|year)|курс\s*[1-4]|[1-4]\s*[-_]?\s*курс\w*)\s*$",
    re.IGNORECASE,
)

_DEPARTMENT_PATTERN = re.compile(
    r"^\s*(кафедра|департамент|department|факультет|faculty|центр|centre)\b",
    re.IGNORECASE,
)

_LANGUAGE_PATTERN = re.compile(
    r"^\s*("
    r"каз[а-яёa-z]*(\s+(бөлімі|бөлім|отделение|тобы))?|"
    r"рус[а-яёa-z]*(\s+(бөлімі|бөлім|отделение|группа))?|"
    r"англ[а-яёa-z]*(\s+(бөлімі|бөлім|отделение))?|"
    r"eng[a-z]*(\s+(section|group|division))?|"
    r"kz|ru|en|qaz|"
    r"қазақ\s+бөлімі|орыс\s+бөлімі|"
    r"очное|заочное|дистанционн\w*|дот|"
    r"20\d\d"
    r")\s*$",
    re.IGNORECASE,
)

_TEACHER_INITIALS = re.compile(
    r"^[А-ЯЁA-Z][а-яёa-z]+(\s+[А-ЯЁA-Z]\.\s*[А-ЯЁA-Z]\.?|[_-][А-ЯЁA-Z]\.[А-ЯЁA-Z]\.?)$|"
    r"^[А-ЯЁA-Z]\.\s*[А-ЯЁA-Z]\.?\s+[А-ЯЁA-Z][а-яёa-z]+$",
    re.IGNORECASE,
)


class FolderRole(Enum):
    WRAPPER = auto()
    DEGREE = auto()
    YEAR = auto()
    DEPARTMENT = auto()
    PROGRAM = auto()
    LANGUAGE = auto()
    TEACHER = auto()
    DISCIPLINE = auto()


def is_wrapper_folder(name: str) -> bool:
    clean = re.sub(r"[\W_]+", " ", name).strip()
    stripped = re.sub(
        r"\b(силлабус\w*|syllabus\w*|силабус\w*|пән\s+бағдарлама\w*|"
        r"осень\w*|күз\w*|весна\w*|көктем\w*|fall\w*|autumn\w*|spring\w*|"
        r"семестр\w*|semester\w*|20\d\d[-–]\d\d\d\d|20\d\d|"
        r"школа\w*|school\w*|мектеп\w*|вшэим|вшп|сцт|шэт|шит|"
        r"materials|материалы|документы|documents|files|файлы)\b",
        "",
        clean,
        flags=re.IGNORECASE,
    ).strip()
    return len(stripped) == 0


def classify_folder_role(name: str) -> FolderRole:
    trimmed = name.strip()
    if is_wrapper_folder(trimmed):
        return FolderRole.WRAPPER
    if _DEGREE_PATTERN.search(trimmed):
        return FolderRole.DEGREE
    if _YEAR_PATTERN.search(trimmed):
        return FolderRole.YEAR
    if _DEPARTMENT_PATTERN.search(trimmed):
        return FolderRole.DEPARTMENT
    if _PROGRAM_FOLDER.search(trimmed) or _CODE_FOLDER.search(trimmed):
        return FolderRole.PROGRAM
    if _LANGUAGE_PATTERN.search(trimmed):
        return FolderRole.LANGUAGE
    if _TEACHER_INITIALS.search(trimmed):
        return FolderRole.TEACHER
    return FolderRole.DISCIPLINE


_TRAILING_TAGS = re.compile(
    r"[\s_\-–—]+("
    r"20\d\d[-–/]\d\d\d\d|20\d\d|осень|весна|күз|көктем|fall|spring|autumn|семестр\s*\d|\d\s*семестр|"
    r"каз[а-яёa-z]*|рус[а-яёa-z]*|англ[а-яёa-z]*|eng[a-z]*|kz|ru|en|qaz|"
    r"v\d+|\d+[-_]?версия|"
    r"[А-ЯЁA-Z][а-яёa-z]+(\s+[А-ЯЁA-Z]\.\s*[А-ЯЁA-Z]\.?|\.[А-ЯЁA-Z]\.[А-ЯЁA-Z]\.?)"
    r")[\s_\-–—]*$",
    re.IGNORECASE,
)


def extract_discipline_from_filename(filename: str) -> str:
    stem = Path(filename).stem.strip()
    # 1. Remove brackets with language, degree, teacher, or year tags
    cleaned = re.sub(
        r"\((?:каз\w*|рус\w*|англ\w*|eng\w*|kz\b|ru\b|en\b|qaz\w*|маг\w*|бак\w*|phd\b|дот\b|дневное|заочное|20\d\d(?:-20\d\d)?|[^)]*)\)",
        " ",
        stem,
        flags=re.IGNORECASE,
    )
    # 2. Remove syllabus prefixes
    cleaned = re.sub(
        r"^(силлабус[а-яёa-z]*|syllabus[a-z]*|силабус[а-яёa-z]*|пән\s+бағдарламасы|рабочая\s+программа|course\s+outline)\s*(по\s+дисциплине\s*|пәні\s+бойынша\s*|по\s*|on\s*|of\s*|[.:_\-–—]|\s)\s*",
        "",
        cleaned,
        flags=re.IGNORECASE,
    )
    cleaned = re.sub(
        r"^(силлабус[а-яёa-z]*|syllabus[a-z]*|силабус[а-яёa-z]*)\s+", "", cleaned, flags=re.IGNORECASE
    )
    # 3. Repeatedly strip trailing semester, language, and version tags
    while True:
        stripped = _TRAILING_TAGS.sub("", cleaned)
        if stripped == cleaned:
            break
        cleaned = stripped

    cleaned = re.sub(r"^[\W_]+|[\W_]+$", "", cleaned).strip()
    cleaned = re.sub(r"\s+", " ", cleaned)
    if len(cleaned) >= 2 and not is_wrapper_folder(cleaned):
        return nfc(cleaned)
    return nfc(stem)


def resolve_file_location(entry: DriveEntry, chain: list[DriveEntry]) -> tuple[str, str, str | None]:
    """Resolves (discipline, course_id, program_folder) for a file given its ancestor folder chain."""
    effective_chain = list(chain)
    while effective_chain and is_wrapper_folder(effective_chain[0].name):
        effective_chain.pop(0)

    program_folder = None
    for f in effective_chain:
        if _PROGRAM_FOLDER.search(f.name) or _CODE_FOLDER.search(f.name):
            program_folder = f.name
            break

    if not effective_chain:
        discipline = extract_discipline_from_filename(entry.name)
        return discipline, "", program_folder

    idx = len(effective_chain) - 1
    while idx >= 0:
        folder = effective_chain[idx]
        role = classify_folder_role(folder.name)
        if role in (FolderRole.LANGUAGE, FolderRole.YEAR, FolderRole.TEACHER, FolderRole.WRAPPER):
            idx -= 1
        else:
            break

    if idx >= 0:
        candidate = effective_chain[idx]
        role = classify_folder_role(candidate.name)
        if role == FolderRole.DISCIPLINE:
            return candidate.name, candidate.id, program_folder
        discipline = extract_discipline_from_filename(entry.name)
        return discipline, candidate.id, program_folder

    discipline = extract_discipline_from_filename(entry.name)
    return discipline, effective_chain[-1].id, program_folder


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
    program_folder: str | None = None

    @property
    def course_name(self) -> str:
        stem = Path(self.entry.name).stem.strip()
        if stem.casefold() == self.discipline.casefold():
            return nfc(self.discipline)[:200]
        return nfc(f"{self.discipline} · {stem}")[:200]


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
                    "program_folder": f.program_folder,
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
                    f.get("program_folder"),
                )
                for f in data.get("found", [])
            ],
        )

    def taken(self, excluded: frozenset[str]) -> list[FoundFile]:
        """The files a read takes: everything but what the School excluded, file by file or a course folder."""
        return [f for f in self.found if f.entry.id not in excluded and f.course_id not in excluded]


def list_folder_tree(client: DriveClient, root_id: str) -> Listing:
    """Blocking. Adaptively finds every syllabus file below root_id across diverse school layouts,
    what was skipped and why."""
    found: list[FoundFile] = []
    skipped: list[dict[str, str]] = []
    folder_info: dict[str, tuple[str, str | None, str, list[DriveEntry]]] = {}

    root_entries = client.list_folder(root_id)
    pending_files: list[tuple[DriveEntry, list[DriveEntry], str]] = []
    current_level_folders: list[DriveEntry] = []

    for entry in root_entries:
        name = nfc(entry.name.strip())
        if entry.is_folder:
            if REFERENCE_FOLDER.search(name):
                skipped.append(
                    {"path": f"{name}/", "reason": "study plans, archive or guidelines, not syllabi"}
                )
            else:
                folder_info[entry.id] = (name, None, name, [entry])
                current_level_folders.append(entry)
        else:
            pending_files.append((entry, [], name))

    depth = 0
    while current_level_folders and depth < 10:
        depth += 1
        folder_ids = [f.id for f in current_level_folders]
        next_level_folders: list[DriveEntry] = []

        children = client.list_children(folder_ids)
        for child in children:
            parent_id = child.parent_id or ""
            if parent_id not in folder_info:
                continue
            parent_name, _, parent_path, parent_chain = folder_info[parent_id]
            child_name = nfc(child.name.strip())
            child_path = f"{parent_path}/{child_name}"

            if child.is_folder:
                if REFERENCE_FOLDER.search(child_name):
                    skipped.append(
                        {
                            "path": f"{child_path}/",
                            "reason": "study plans, archive or guidelines, not syllabi",
                        }
                    )
                else:
                    child_chain = parent_chain + [child]
                    folder_info[child.id] = (child_name, parent_id, child_path, child_chain)
                    next_level_folders.append(child)
            else:
                pending_files.append((child, parent_chain, child_path))

        current_level_folders = next_level_folders

    courses_map: dict[str, str] = {}
    seen_file_ids: set[str] = set()

    for entry, chain, file_path in pending_files:
        if entry.app_version_id:
            continue
        if entry.id in seen_file_ids:
            continue
        seen_file_ids.add(entry.id)

        file_name = nfc(entry.name.strip())

        if not entry.content_type:
            skipped.append({"path": file_path, "reason": "not a .docx, .pdf or Google Doc"})
            continue

        if NON_SYLLABUS_FILE.search(file_name):
            skipped.append(
                {"path": file_path, "reason": "not a syllabus (readme, template or administrative file)"}
            )
            continue

        discipline, course_id, program_folder = resolve_file_location(entry, chain)
        found.append(
            FoundFile(
                discipline=discipline,
                path=file_path,
                entry=entry,
                course_id=course_id,
                program_folder=program_folder,
            )
        )
        course_key = course_id or discipline
        courses_map[course_key] = discipline

    parent_ids = {entry_tuple[1] for entry_tuple in folder_info.values() if entry_tuple[1]}
    for fid, (fname, fparent, fpath, fchain) in folder_info.items():
        if fid not in parent_ids and classify_folder_role(fname) == FolderRole.DISCIPLINE:
            if fid not in courses_map:
                courses_map[fid] = fname

    courses = sorted(courses_map.items(), key=lambda c: c[1])
    return Listing(courses, found, skipped)


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
    outside = await external_models(session, [s.id for s in known.values()])
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
                "critical_issues": s.critical_issues if s else None,
                "major_issues": s.major_issues if s else None,
                "external_model": outside.get(s.id) if s else None,
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
    syllabus.critical_issues = None
    syllabus.major_issues = None
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

    # Which programs list these courses (narxoz.edu.kz course lists); new and renamed courses may change.
    from app.services import program_links

    if by_slug:
        ids = await session.scalars(sa.select(Course.id).where(Course.slug.in_(list(by_slug))))
        hints = {
            course.id: f.program_folder
            for slug, f in by_slug.items()
            if f.program_folder and (course := courses.get(slug))
        }
        await program_links.link_by_name(session, list(ids), program_hints=hints)

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
        # The School's placeholder program (no code), never one imported from narxoz.edu.kz: which real
        # programs a Drive course belongs to is decided separately, and a course can belong to several.
        program_id = await session.scalar(
            sa.select(Program.id)
            .where(Program.school_id == school.id)
            .order_by(Program.code.is_not(None), Program.name)
            .limit(1)
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

            account = (await drive_admin.status()).get("client_email")
            report.error = await explain_folder_error(exc, account, root)
    async with session_scope() as session:
        folder = await session.get(UploadFolder, folder_id)
        if folder is not None:
            folder.read_at, folder.read_error = datetime.now(UTC), report.error
            if not report.error:
                try:  # several files of one listed course: the newest counts, the others wait for the School
                    from app.services import syllabus_coverage

                    await syllabus_coverage.resolve(session, folder.semester, folder.school_id)
                except Exception:
                    log.warning("could not settle duplicate syllabi of folder %s", folder_id, exc_info=True)
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
        reports = [await sync_folder(f) for f in folder_ids]
    finally:
        await cache.client().delete(_LOCK)
    # Open the new files now, so the cost window before a check finds them counted (services.cost_estimate).
    try:
        from app.workers import dispatch

        await dispatch.count_new_files()
    except Exception:
        log.warning("could not start counting the new files", exc_info=True)
    return reports


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
