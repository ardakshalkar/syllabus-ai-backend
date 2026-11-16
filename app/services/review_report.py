"""Generated review: the analysis report as a PDF, and sending it to the Provost Office.

The report itself is `analysis.report` (the same data as the printable page in the app); this module only
sets it in a PDF and mails it. The PDF is made on the server so it can be attached to an email; the page in
the app stays the way to read it on screen.
"""

import asyncio
import logging
import smtplib
import uuid
from datetime import datetime
from email.message import EmailMessage
from pathlib import Path
from typing import Literal

import sqlalchemy as sa
from fpdf import FPDF
from fpdf.enums import XPos, YPos
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.rbac import Principal
from app.config import get_settings
from app.enums import Role
from app.models import Program, ReviewReport, RoleAssignment, School, User
from app.services import analysis

log = logging.getLogger(__name__)

Include = Literal["summary", "critical", "all"]

# Regular and bold faces, tried in order. DejaVu covers Cyrillic and the Kazakh letters; so does Arial.
_FONTS = [
    ("DejaVuSans.ttf", "DejaVuSans-Bold.ttf"),
    ("arial.ttf", "arialbd.ttf"),
]
_FONT_DIRS = ["/usr/share/fonts/truetype/dejavu", "/usr/share/fonts/dejavu", "C:/Windows/Fonts"]


class NoFont(RuntimeError):
    pass


class NoRecipient(RuntimeError):
    pass


def _fonts() -> tuple[Path, Path]:
    configured = get_settings().report_font_dir
    for folder in ([configured] if configured else []) + _FONT_DIRS:
        for regular, bold in _FONTS:
            r, b = Path(folder) / regular, Path(folder) / bold
            if r.is_file() and b.is_file():
                return r, b
    raise NoFont(
        "No font for the PDF report: install DejaVu (fonts-dejavu-core) or set REPORT_FONT_DIR "
        "to a folder with DejaVuSans.ttf and DejaVuSans-Bold.ttf."
    )


def _short_school(name: str) -> str:
    return name.removeprefix("School of ").strip()


class _Pdf(FPDF):
    def __init__(self, footer_text: str):
        super().__init__(format="A4")
        self.footer_text = footer_text
        regular, bold = _fonts()
        self.add_font("Body", "", str(regular))
        self.add_font("Body", "B", str(bold))
        self.set_auto_page_break(auto=True, margin=16)
        self.set_margins(16, 16, 16)

    def footer(self) -> None:
        self.set_y(-12)
        self.set_font("Body", "", 8)
        self.set_text_color(130, 130, 130)
        self.cell(0, 6, f"{self.footer_text} · {self.page_no()}", align="R")

    def line_text(self, text: str, size: float = 10, bold: bool = False, color=(30, 30, 30), gap: float = 1.5):
        self.set_font("Body", "B" if bold else "", size)
        self.set_text_color(*color)
        self.multi_cell(0, size * 0.5, text, align="L", new_x=XPos.LMARGIN, new_y=YPos.NEXT)
        self.ln(gap)


_SEVERITY_COLOR = {"critical": (190, 40, 40), "major": (170, 110, 10), "minor": (110, 110, 110)}


def _counts(c: dict) -> str:
    parts = [f"{c['critical']} critical" if c["critical"] else "", f"{c['major']} major" if c["major"] else "",
             f"{c['minor']} minor" if c["minor"] else ""]
    return " · ".join(p for p in parts if p) or "no findings"


def render_pdf(report: dict) -> bytes:
    """Set the report in a PDF: a summary page, then each course on a page of its own."""
    semester = report["semester"]
    scope = _short_school(report["school_name"]) if report.get("school_name") else "Whole university"
    pdf = _Pdf(f"Narxoz University · Syllabus review · {semester} · {scope}")
    pdf.add_page()
    t = report["totals"]
    generated = report["generated_at"]
    when = generated.strftime("%d %b %Y, %H:%M") if isinstance(generated, datetime) else str(generated)
    pdf.line_text(f"Narxoz University · Syllabus review · {semester}", 9, color=(120, 120, 120))
    pdf.line_text(scope, 20, bold=True, gap=1)
    pdf.line_text(f"{t['found']} syllabi · {t['checked']} checked · generated {when}", 9, color=(120, 120, 120), gap=5)

    for label, value, color in [
        ("Syllabi", t["found"], (30, 30, 30)),
        ("With critical findings", t["with_critical"], _SEVERITY_COLOR["critical"]),
        ("Major only", t["major_only"], _SEVERITY_COLOR["major"]),
        ("No AI result yet", t["cannot_check"] + t["not_checked"], (30, 30, 30)),
    ]:
        pdf.set_font("Body", "", 10)
        pdf.set_text_color(90, 90, 90)
        pdf.cell(60, 7, label)
        pdf.set_font("Body", "B", 11)
        pdf.set_text_color(*color)
        pdf.cell(0, 7, str(value), new_x=XPos.LMARGIN, new_y=YPos.NEXT)
    pdf.ln(4)

    if not report.get("school_id") and len(report["schools"]) > 1:
        pdf.line_text("By School", 13, bold=True)
        for s in report["schools"]:
            pdf.set_font("Body", "", 10)
            pdf.set_text_color(30, 30, 30)
            pdf.cell(110, 6, _short_school(s["school_name"]))
            pdf.cell(25, 6, f"{s['found']}", align="R")
            pdf.cell(0, 6, f"{s['with_critical']} critical", align="R", new_x=XPos.LMARGIN, new_y=YPos.NEXT)
        pdf.ln(4)

    issues = sorted(report["issues"], key=lambda i: (not i["critical"], -i["syllabi"]))[:10]
    if issues:
        pdf.line_text("Most common findings", 13, bold=True)
        for n, i in enumerate(issues, 1):
            mark = "  [critical]" if i["critical"] else ""
            pdf.line_text(f"{n}. {i['criterion_id']} · {i['name']} — {i['syllabi']} ({i['percent']}%){mark}", 10, gap=0.5)
        pdf.ln(3)

    courses = report["courses"]
    if courses:
        title = "Courses with critical findings" if report["include"] == "critical" else "Courses"
        pdf.line_text(f"{title} ({len(courses)})", 13, bold=True)
        for n, c in enumerate(courses, 1):
            pdf.line_text(f"{n}. {c['course_name']} — {_counts(c)}", 10, gap=0.5)
    pdf.ln(3)
    pdf.line_text(
        "Findings are suggestions from the AI check for the professor to review, not an approval decision.",
        8, color=(120, 120, 120),
    )

    for c in courses:
        pdf.add_page()
        pdf.line_text(c["course_name"], 15, bold=True, gap=1)
        who = c["professor_name"] or "No professor matched"
        pdf.line_text(f"{who} · {c['language']} · {_short_school(c['school_name'])} · {c['program_name']}", 9,
                      color=(120, 120, 120), gap=1)
        pdf.line_text(_counts(c), 9, bold=True, color=(90, 90, 90), gap=4)
        for severity in ("critical", "major", "minor"):
            found = [f for f in c["findings"] if f["severity"] == severity]
            if not found:
                continue
            pdf.line_text(f"{severity.capitalize()} · {len(found)}", 12, bold=True, color=_SEVERITY_COLOR[severity])
            for f in found:
                if severity == "minor":
                    pdf.line_text(f"{f['criterion_id']} {f['name']} — {f['recommendation'] or f['explanation']}", 9, gap=1)
                    continue
                pdf.line_text(f"{f['criterion_id']} · {f['principle_name']}", 8, color=(120, 120, 120), gap=0.5)
                pdf.line_text(f["name"], 10, bold=True, gap=0.5)
                if f["explanation"]:
                    pdf.line_text(f["explanation"], 9.5, gap=0.8)
                if f["evidence"]:
                    pdf.set_x(pdf.l_margin + 4)
                    pdf.set_font("Body", "", 9)
                    pdf.set_text_color(90, 90, 90)
                    pdf.multi_cell(0, 4.5, f"“{f['evidence']}”", align="L", new_x=XPos.LMARGIN, new_y=YPos.NEXT)
                    pdf.ln(0.8)
                if f["recommendation"]:
                    pdf.line_text(f"Fix: {f['recommendation']}", 9.5, color=(20, 90, 60), gap=3)
    return bytes(pdf.output())


def filename(report: dict) -> str:
    scope = _short_school(report["school_name"]) if report.get("school_name") else "University"
    slug = "-".join(f"{scope} {report['semester']}".split())
    return f"Syllabus-review-{slug}.pdf"


async def build(
    session: AsyncSession, principal: Principal, *, semester: str, school_id: uuid.UUID | None, include: Include
) -> tuple[dict, bytes, str]:
    report = await analysis.report(session, principal, semester=semester, school_id=school_id, include=include)
    pdf = await asyncio.to_thread(render_pdf, report)
    return report, pdf, filename(report)


async def _recipients(session: AsyncSession) -> list[str]:
    configured = [a.strip() for a in get_settings().provost_office_email.split(",") if a.strip()]
    if configured:
        return configured
    rows = await session.scalars(
        sa.select(User.email).join(RoleAssignment, RoleAssignment.user_id == User.id).where(
            RoleAssignment.role == Role.provost, User.is_active.is_(True)
        )
    )
    return sorted(set(rows))


def _send_smtp(to: list[str], subject: str, body: str, pdf: bytes, name: str) -> None:
    s = get_settings()
    message = EmailMessage()
    message["From"], message["To"], message["Subject"] = s.email_from, ", ".join(to), subject
    message.set_content(body)
    message.add_attachment(pdf, maintype="application", subtype="pdf", filename=name)
    with smtplib.SMTP(s.smtp_host, s.smtp_port, timeout=60) as smtp:
        if s.smtp_starttls:
            smtp.starttls()
        if s.smtp_user:
            smtp.login(s.smtp_user, s.smtp_password)
        smtp.send_message(message)


async def send(
    session: AsyncSession, principal: Principal, *, semester: str, school_id: uuid.UUID | None, include: Include
) -> ReviewReport:
    """Make the PDF and mail it to the Provost Office, then record that it went."""
    to = await _recipients(session)
    if not to:
        raise NoRecipient("Nobody to send it to: set PROVOST_OFFICE_EMAIL, or give someone the provost role.")
    report, pdf, name = await build(session, principal, semester=semester, school_id=school_id, include=include)
    scope = _short_school(report["school_name"]) if report.get("school_name") else "the whole university"
    t = report["totals"]
    subject = f"Syllabus review · {scope} · {semester}"
    body = (
        f"The syllabus review for {scope}, {semester}, is attached.\n\n"
        f"{t['found']} syllabi, {t['checked']} checked by the AI, {t['with_critical']} with critical findings.\n"
        f"Sent by {principal.user.name} from Syllabus AI: {get_settings().frontend_url}\n"
    )
    settings = get_settings()
    if settings.email_backend == "console":
        outbox = Path("var/outbox")
        await asyncio.to_thread(outbox.mkdir, parents=True, exist_ok=True)
        await asyncio.to_thread((outbox / name).write_bytes, pdf)
        log.info("EMAIL to=%s subject=%s attachment=%s (%d bytes, kept in %s)\n%s",
                 ", ".join(to), subject, name, len(pdf), outbox / name, body)
    else:
        await asyncio.to_thread(_send_smtp, to, subject, body, pdf, name)
    row = ReviewReport(
        semester=semester, school_id=school_id, include=include, courses=len(report["courses"]),
        recipients=to, filename=name, sent_by=principal.id,
    )
    session.add(row)
    await session.flush()
    return row


async def sent(session: AsyncSession, principal: Principal, *, semester: str) -> list[dict]:
    """Reports sent this semester, newest first, for the Schools this person may see."""
    query = (
        sa.select(ReviewReport, User.name, School.name)
        .join(User, User.id == ReviewReport.sent_by)
        .outerjoin(School, School.id == ReviewReport.school_id)
        .where(ReviewReport.semester == semester)
        .order_by(ReviewReport.created_at.desc())
    )
    if not principal.university_wide:
        # A School administrator's Schools, and the Schools a Head of Program's programs belong to.
        heads = sa.select(Program.school_id).where(Program.id.in_(principal.program_ids))
        query = query.where(sa.or_(ReviewReport.school_id.in_(principal.school_ids), ReviewReport.school_id.in_(heads)))
    return [
        {
            "id": r.id, "semester": r.semester, "school_id": r.school_id, "school_name": school,
            "include": r.include, "courses": r.courses, "recipients": r.recipients, "filename": r.filename,
            "sent_by": who, "sent_at": r.created_at,
        }
        for r, who, school in (await session.execute(query)).all()
    ]
