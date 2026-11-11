"""Enum values match the strings used by the prototype frontend (lib/data.ts) so the UI can bind directly."""

from enum import StrEnum


class Role(StrEnum):
    admin = "admin"  # system administrator: everything provost and KOK see, plus users, roles and the catalog
    provost = "provost"
    school_admin = "school_admin"
    program_head = "program_head"
    kok = "kok"
    professor = "professor"


class AiStatus(StrEnum):
    not_checked = "Not checked"
    processing = "Processing"
    checked = "Checked"
    needs_revision = "Needs revision"
    cannot_check = "Cannot check"


class ApprovalStage(StrEnum):
    draft = "Draft"
    ai_checked = "AI Checked"
    professor_revised = "Professor Revised"
    head_review = "Head of Program Review"
    kok_review = "KOK Review"
    approved = "Approved"


class ResultStatus(StrEnum):
    compliant = "Compliant"
    partial = "Partially compliant"
    not_compliant = "Not compliant"
    cannot_verify = "Cannot verify"


ISSUE_STATUSES = (ResultStatus.partial, ResultStatus.not_compliant)


class EvaluationType(StrEnum):
    deterministic = "Deterministic"
    ai_semantic = "AI semantic"
    external_context = "External context required"


class Trigger(StrEnum):
    provost_session = "Provost session"
    professor_manual = "Professor manual check"
    scheduled_scan = "Scheduled scan"
    upload = "Upload"


class RunStatus(StrEnum):
    queued = "Queued"
    processing = "Processing"
    completed = "Completed"
    failed = "Failed"


class VersionSource(StrEnum):
    upload = "upload"
    improve = "improve"
    wizard = "wizard"
    seed = "seed"
    drive = "drive"


class SessionScope(StrEnum):
    university = "university"
    school = "school"
    program = "program"
    folder = "folder"
    file = "file"


class SessionStatus(StrEnum):
    scheduled = "Scheduled"
    running = "Running"
    paused = "Paused"
    completed = "Completed"


class SessionItemStatus(StrEnum):
    queued = "Queued"
    processing = "Processing"
    done = "Done"
    failed = "Failed"


class CheckKind(StrEnum):
    """Session wizard checkboxes ("Evaluation configuration" step)."""

    technical = "Technical structure check"
    criteria = "Quality criteria check"
    evidence = "Evidence extraction"
    recommendations = "Recommendations"
    program_alignment = "Program alignment checks"
    library = "Library verification"
