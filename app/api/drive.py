"""Google Drive connection, managed in the app by the Provost Office (Schools & Drive screen).
School administrators and KOK can see its status; only the Provost Office changes or tests it."""

import sqlalchemy as sa
from fastapi import APIRouter, HTTPException, UploadFile, status

from app.auth.rbac import Principal, ensure_manages_school, forbidden, manages_school
from app.deps import CurrentUser, DbSession
from app.enums import Role
from app.models import School, Syllabus, UploadFolder
from app.schemas.catalog import (
    DriveCheckIn,
    DriveCheckOut,
    DriveSettingsIn,
    DriveSetupIn,
    DriveSetupOut,
    DriveStatusOut,
    DriveSyncIn,
    DriveSyncOut,
)
from app.services import drive_admin, drive_catalog
from app.services.drive_admin import DriveSettingsError
from app.services.drive_setup import setup_shared_folder
from app.workers import dispatch

router = APIRouter(prefix="/drive", tags=["drive"])


def _viewer(principal: Principal) -> None:
    if not principal.has(Role.provost, Role.kok, Role.school_admin):
        raise forbidden("Only the Provost Office, KOK and School administrators see the Drive connection")


def _admin(principal: Principal) -> None:
    if not principal.has(Role.provost):
        raise forbidden("Only the Provost Office can connect Google Drive")


def _unprocessable(reason: Exception | str) -> HTTPException:
    return HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, str(reason))


@router.get("", response_model=DriveStatusOut)
async def get_status(principal: CurrentUser):
    _viewer(principal)
    return await drive_admin.status()


@router.post("/credentials", response_model=DriveStatusOut)
async def upload_key(file: UploadFile, principal: CurrentUser):
    """Upload the service account key (JSON). From now on the app uses Google Drive with it."""
    _admin(principal)
    data = await file.read(drive_admin.MAX_KEY_BYTES + 1)
    try:
        await drive_admin.save_key(data, principal.user.email)
    except DriveSettingsError as exc:
        raise _unprocessable(exc) from exc
    return await drive_admin.status()


@router.delete("/credentials", response_model=DriveStatusOut)
async def delete_key(principal: CurrentUser):
    """Delete the uploaded key; Drive goes back to the server's .env settings."""
    _admin(principal)
    await drive_admin.forget_key(principal.user.email)
    return await drive_admin.status()


@router.patch("", response_model=DriveStatusOut)
async def update(body: DriveSettingsIn, principal: CurrentUser):
    _admin(principal)
    try:
        await drive_admin.update_settings(
            principal.user.email,
            shared_folder_id=body.shared_folder_id,
            impersonate_user=body.impersonate_user,
            read_only=body.read_only,
        )
    except DriveSettingsError as exc:
        raise _unprocessable(exc) from exc
    return await drive_admin.status()


@router.post("/check", response_model=DriveCheckOut)
async def check(body: DriveCheckIn, principal: CurrentUser):
    """Test the key and the folders, like `python -m app.cli drive-check`."""
    _admin(principal)
    return await drive_admin.check(body.folder_ids, body.write)


@router.post("/setup", response_model=DriveSetupOut)
async def setup(body: DriveSetupIn, principal: CurrentUser):
    """Link every School to its subfolder of the shared folder (created when missing), like `drive-setup`."""
    _admin(principal)
    shared = (await drive_admin.status())["shared_folder_id"]
    if not shared:
        raise _unprocessable("Set the shared folder first")
    try:
        report = await setup_shared_folder(shared, body.semester, create=body.create, dry_run=body.dry_run)
    except Exception as exc:
        from app.storage.drive import explain_error

        raise _unprocessable(explain_error(exc)) from exc
    return DriveSetupOut(
        shared_folder_id=shared,
        semester=body.semester,
        dry_run=body.dry_run,
        links=[
            {"school": link.school, "action": link.action, "drive_folder_id": link.drive_folder_id}
            for link in report.links
        ],
        folders_created=report.folders_created_in_db,
        folders_updated=report.folders_updated_in_db,
    )


@router.post("/sync", status_code=status.HTTP_202_ACCEPTED)
async def start_sync(body: DriveSyncIn, principal: CurrentUser) -> dict:
    """Re-read the Schools' Drive folders now (also done by celery beat every DRIVE_SYNC_INTERVAL_S).
    A School administrator refreshes their School; the Provost Office and KOK any or all."""
    if body.school_id is not None:
        ensure_manages_school(principal, body.school_id)
    elif not principal.has(Role.provost, Role.kok):
        raise forbidden("Choose your School to refresh")
    if await drive_catalog.sync_running():
        raise HTTPException(
            status.HTTP_409_CONFLICT, "Google Drive is being read already: wait for it to finish"
        )
    await drive_catalog.mark_requested()
    await dispatch.sync_drive_catalog(body.school_id)
    return {"started": True}


@router.get("/sync", response_model=DriveSyncOut)
async def sync_status(principal: CurrentUser, session: DbSession):
    """The Schools' Drive folders this user manages, with the last sync of each."""
    _viewer(principal)
    all_rows = (
        await session.execute(
            sa.select(UploadFolder, School.name)
            .join(School, School.id == UploadFolder.school_id)
            .where(UploadFolder.drive_folder_id.is_not(None))
            .order_by(School.name, UploadFolder.semester)
        )
    ).all()
    rows = [(f, name) for f, name in all_rows if manages_school(principal, f.school_id)]
    counts = dict(
        (
            await session.execute(
                sa.select(Syllabus.folder_id, sa.func.count())
                .where(Syllabus.folder_id.in_([f.id for f, _ in rows]), Syllabus.drive_missing_at.is_(None))
                .group_by(Syllabus.folder_id)
            )
        ).all()
    )
    reports = await drive_catalog.last_reports([f.id for f, _ in rows])
    return DriveSyncOut(
        running=await drive_catalog.sync_running(),
        folders=[
            {
                "folder_id": f.id,
                "school_id": f.school_id,
                "school_name": name,
                "semester": f.semester,
                "path": f.path,
                "drive_folder_id": f.drive_folder_id,
                "syllabi": counts.get(f.id, 0),
                "last_sync": reports.get(f.id),
            }
            for f, name in rows
        ],
    )
