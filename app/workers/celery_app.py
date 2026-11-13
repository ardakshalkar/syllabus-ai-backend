from celery import Celery

from app.config import get_settings

settings = get_settings()

celery_app = Celery("syllabus", broker=settings.redis_url, include=["app.workers.tasks"])
celery_app.conf.update(
    task_ignore_result=True,
    task_acks_late=True,
    task_reject_on_worker_lost=True,
    worker_prefetch_multiplier=1,
    task_default_queue="default",
    # LLM-bound work gets its own queue so its worker concurrency can match vLLM capacity.
    task_routes={"syllabus.check_run": {"queue": "ai"}, "syllabus.precheck": {"queue": "ai"}},
    beat_schedule={
        "start-due-sessions": {"task": "syllabus.start_due_sessions", "schedule": 60.0},
        "send-pending-emails": {"task": "syllabus.send_pending_emails", "schedule": 60.0},
        "sync-drive-files": {"task": "syllabus.sync_drive_files", "schedule": 60.0},
        # Syllabi read from Google Drive: the catalog of the Schools' folders (services/drive_catalog.py).
        "sync-drive-catalog": {
            "task": "syllabus.sync_drive_catalog",
            "schedule": float(settings.drive_sync_interval_s),
        },
        # Runs left Processing by a killed worker; see app/services/run_live.py.
        "recover-stale-runs": {"task": "syllabus.recover_stale_runs", "schedule": 120.0},
    },
    timezone="UTC",
)
