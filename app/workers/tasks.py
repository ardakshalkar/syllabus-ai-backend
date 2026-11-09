import uuid

from app.workers import runtime
from app.workers.celery_app import celery_app


@celery_app.task(name="syllabus.check_run")
def check_run(run_id: str) -> None:
    from app.services.checks import execute_next

    runtime.run(execute_next, uuid.UUID(run_id))


@celery_app.task(name="syllabus.recover_stale_runs")
def recover_stale_runs() -> None:
    from app.services.checks import recover_stale_runs as recover

    runtime.run(recover)


@celery_app.task(name="syllabus.precheck")
def precheck(job_id: str) -> None:
    from app.services.precheck import run_job

    runtime.run(run_job, job_id)


@celery_app.task(name="syllabus.start_due_sessions")
def start_due_sessions() -> None:
    from app.services.sessions import start_due_sessions as start_due

    runtime.run(start_due)


@celery_app.task(name="syllabus.send_pending_emails")
def send_pending_emails() -> None:
    from app.services.notifications import send_pending_emails as send

    runtime.run(send)


@celery_app.task(name="syllabus.sync_drive_files")
def sync_drive_files() -> None:
    from app.services.drive_sync import sync_pending_files

    runtime.run(sync_pending_files)


@celery_app.task(name="syllabus.sync_drive_catalog")
def sync_drive_catalog(school_id: str | None = None, scheduled: bool = True) -> None:
    from app.services import drive_catalog

    if scheduled:
        runtime.run(drive_catalog.scheduled_sync)
    else:
        runtime.run(drive_catalog.sync_all, uuid.UUID(school_id) if school_id else None)
