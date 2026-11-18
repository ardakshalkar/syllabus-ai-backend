"""Generated review: the analysis report as a PDF, and sending it to the Provost Office.

The report itself is `analysis.report` (the same data as the printable page in the app); this module only
sets it in a PDF and mails it. The PDF is made on the server so it can be attached to an email; the page in
the app stays the way to read it on screen.
"""

import asyncio
import json
import logging
import re
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
Lang = Literal["en", "ru", "kk"]

# The report's words in Russian and Kazakh: its labels, the criterion and principle names, the School names.
# Generated from the app's own translations (lib/i18n.ts, lib/i18n-kk.ts) so the PDF says what the screens say.
_TEXTS: dict[str, dict[str, str]] = json.loads(
    (Path(__file__).with_name("report_texts.json")).read_text("utf-8")
)

# The five Schools by the short English name the screens show; the server stores some names in English and some
# in Russian, depending on how each School was created. The same list as the app's lib/schools.ts.
_SCHOOLS = {
    "школа экономики и менеджмента": "Economics and Management",
    "school of economics and management": "Economics and Management",
    "школа цифровых технологий": "Digital Technologies",
    "school of digital technologies": "Digital Technologies",
    "школа социальных и прикладных наук": "Social and Applied Sciences",
    "school of social and applied sciences": "Social and Applied Sciences",
    "школа права и государственного управления": "Law and Public Administration",
    "school of law and public administration": "Law and Public Administration",
    "школа базовых и междисциплинарных программ": "Basic and Interdisciplinary Programs",
    "school of basic and interdisciplinary programs": "Basic and Interdisciplinary Programs",
}
_LANGUAGE_NAMES = {"en": "English", "ru": "Russian", "kk": "Kazakh"}


def translator(lang: Lang):
    """`text` in `lang`, or as it is when there is no translation (English, or a name nobody translated)."""
    table = _TEXTS.get(lang, {})
    return lambda text: table.get(text, text) if text else text


# Regular and bold faces, tried in order. DejaVu covers Cyrillic and the Kazakh letters; so does Arial.
_FONTS = [
    ("DejaVuSans.ttf", "DejaVuSans-Bold.ttf"),
    ("arial.ttf", "arialbd.ttf"),
]
_FONT_DIRS = ["/usr/share/fonts/truetype/dejavu", "/usr/share/fonts/dejavu", "C:/Windows/Fonts"]
# A backup for the characters DejaVu does not have, such as Chinese quoted from a Chinese-language syllabus:
# (folder, regular, bold). WenQuanYi Micro Hei is the Debian package fonts-wqy-microhei; Microsoft YaHei ships
# with Windows. Without one the report still renders, with those characters left blank.
_CJK_FONTS = [
    ("/usr/share/fonts/truetype/wqy", "wqy-microhei.ttc", "wqy-microhei.ttc"),
    ("C:/Windows/Fonts", "msyh.ttc", "msyhbd.ttc"),
]
# Chinese, Japanese and Korean letters and their full-width punctuation: what DejaVu lacks and the backup has.
_CJK = re.compile(r"[⺀-鿿가-힯豈-﫿＀-￯]")


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
    return _SCHOOLS.get(name.strip().lower()) or name.removeprefix("School of ").strip()


def _semester(semester: str, tr) -> str:
    """ "Fall 2026" as "Осень 2026": the season translated, the year kept."""
    season, _, year = semester.partition(" ")
    return f"{tr(season)} {year}".strip()


# Colours, as RGB. Severity: red for critical, amber for major, grey for minor; the same as the app.
_INK, _MUTED, _LINE, _TILE = (31, 31, 31), (110, 110, 110), (226, 229, 233), (245, 246, 248)
_RED, _RED_SOFT, _RED_TEXT = (226, 75, 74), (252, 235, 235), (163, 45, 45)
_AMBER, _AMBER_SOFT, _AMBER_TEXT = (239, 159, 39), (250, 238, 218), (133, 79, 11)
_GREEN, _GREY, _LIGHT = (29, 158, 117), (180, 178, 169), (213, 219, 226)
_FIX_BG, _FIX_TEXT, _FIX_LABEL = (232, 245, 239), (20, 83, 45), (29, 122, 82)
_BRAND = (196, 18, 51)
_SEVERITY_COLOR = {"critical": _RED_TEXT, "major": _AMBER_TEXT, "minor": _MUTED}
_SEVERITY_BAR = {"critical": _RED, "major": _AMBER, "minor": _GREY}
# Line height for a font size in points, in millimetres (1 pt = 0.353 mm, with 1.45 leading).
_LH = 0.353 * 1.45


def _cjk_fonts() -> tuple[Path, Path] | None:
    configured = get_settings().report_font_dir
    candidates = [(configured, r, b) for _, r, b in _CJK_FONTS] if configured else []
    for folder, regular, bold in candidates + _CJK_FONTS:
        r, b = Path(folder) / regular, Path(folder) / bold
        if r.is_file():
            return r, b if b.is_file() else r
    return None


def _has_cjk(report: dict) -> bool:
    """Whether any text in the report needs the backup font: loading it is slow, so most reports skip it."""
    return bool(_CJK.search(json.dumps(report.get("courses", []), ensure_ascii=False, default=str)))


class _Pdf(FPDF):
    def __init__(self, footer_text: str, lang: Lang = "en", backup_font: bool = False):
        super().__init__(format="A4")
        self.footer_text = footer_text
        self.lang = lang
        self.tr = translator(lang)
        regular, bold = _fonts()
        self.add_font("Body", "", str(regular))
        self.add_font("Body", "B", str(bold))
        cjk = _cjk_fonts() if backup_font else None
        if cjk:
            self.add_font("Backup", "", str(cjk[0]))
            self.add_font("Backup", "B", str(cjk[1]))
            self.set_fallback_fonts(["Backup"])
        elif backup_font:
            log.warning("No CJK font for the PDF report: Chinese, Japanese or Korean text will be left blank")
        self.set_auto_page_break(auto=True, margin=18)
        self.set_margins(18, 18, 18)

    @property
    def width(self) -> float:
        return self.w - self.l_margin - self.r_margin

    def footer(self) -> None:
        self.set_y(-12)
        self.set_draw_color(*_LINE)
        self.line(self.l_margin, self.get_y() - 1, self.w - self.r_margin, self.get_y() - 1)
        self.set_font("Body", "", 8.5)
        self.set_text_color(*_MUTED)
        self.cell(0, 6, self.footer_text)
        self.cell(0, 6, str(self.page_no()), align="R")

    def text_block(
        self,
        text: str,
        size: float = 11,
        bold: bool = False,
        color=_INK,
        gap: float = 1.5,
        x: float | None = None,
        w: float = 0,
    ) -> None:
        self.set_font("Body", "B" if bold else "", size)
        self.set_text_color(*color)
        if x is not None:
            self.set_x(x)
        self.multi_cell(w, size * _LH, text, align="L", new_x=XPos.LMARGIN, new_y=YPos.NEXT)
        self.ln(gap)

    def height_of(
        self, text: str, size: float, w: float, bold: bool = False, markdown: bool = False
    ) -> float:
        self.set_font("Body", "B" if bold else "", size)
        lines = self.multi_cell(w, size * _LH, text, dry_run=True, output="LINES", markdown=markdown)
        return len(lines) * size * _LH

    def room(self, h: float) -> None:
        """Start a new page unless `h` millimetres still fit on this one."""
        if self.get_y() + h > self.page_break_trigger:
            self.add_page()

    def heading(self, text: str, size: float = 15, color=_INK, rule: tuple | None = None) -> None:
        self.room(size * _LH + 14)
        self.ln(4)
        self.text_block(text, size, bold=True, color=color, gap=1.5)
        if rule:
            self.set_draw_color(*rule)
            self.set_line_width(0.6)
            self.line(self.l_margin, self.get_y(), self.w - self.r_margin, self.get_y())
            self.set_line_width(0.2)
            self.ln(3)

    def pill(self, x: float, y: float, text: str, fill, color, size: float = 9) -> float:
        """A small rounded label; returns its width."""
        self.set_font("Body", "B", size)
        w = self.get_string_width(text) + 4
        self.set_fill_color(*fill)
        self.rect(x, y, w, size * 0.5, style="F", round_corners=True, corner_radius=1.2)
        self.set_text_color(*color)
        self.set_xy(x, y)
        self.cell(w, size * 0.5, text, align="C")
        return w

    def bar(
        self, x: float, y: float, w: float, h: float, parts: list[tuple[float, tuple]], track=_LINE
    ) -> None:
        self.set_fill_color(*track)
        self.rect(x, y, w, h, style="F", round_corners=True, corner_radius=h / 2)
        total = sum(n for n, _ in parts) or 1
        at = x
        for n, color in parts:
            if n <= 0:
                continue
            seg = w * n / total
            self.set_fill_color(*color)
            self.rect(at, y, seg, h, style="F")
            at += seg


def _counts(c: dict) -> str:
    parts = [
        f"{c['critical']} critical" if c["critical"] else "",
        f"{c['major']} major" if c["major"] else "",
        f"{c['minor']} minor" if c["minor"] else "",
    ]
    return " · ".join(p for p in parts if p) or "no findings"


def _finding_texts(f: dict, lang: Lang) -> dict:
    """The texts the report shows for a finding, in `lang`: the reviewed headline and fix written for the
    criterion, and the AI's own explanation and recommendation (their translation into `lang`, or the
    language the AI wrote them in when there is none yet)."""
    texts = _plain(f.get("texts"))
    translated = _plain(_plain(f.get("translations")).get(lang))
    pick = lambda key: (_plain(texts.get(key)).get(lang) or "").strip()  # noqa: E731
    return {
        "problem": pick("problem"),
        "fix": pick("fix"),
        "explanation": (translated.get("explanation") or f.get("explanation") or "").strip(),
        "recommendation": (translated.get("recommendation") or f.get("recommendation") or "").strip(),
    }


def _plain(value) -> dict:
    """A model or a dict as a dict; anything else as an empty one."""
    if hasattr(value, "model_dump"):
        return value.model_dump()
    return value if isinstance(value, dict) else {}


def _clip(text: str, n: int) -> str:
    return text if len(text) <= n else text[: n - 1].rsplit(" ", 1)[0] + "…"


def _quotes(evidence: str | None) -> bool:
    """Evidence that is only a heading or a scanner stamp ("Scanned with AnyScanner") says nothing."""
    return bool(evidence) and len(evidence.strip()) > 30


def _summary(pdf: _Pdf, report: dict, scope: str) -> None:
    tr = pdf.tr
    t = report["totals"]
    generated = report["generated_at"]
    when = generated.strftime("%d.%m.%Y, %H:%M") if isinstance(generated, datetime) else str(generated)
    semester = _semester(report["semester"], tr)
    pdf.text_block(
        f"{tr('Narxoz University')} · {tr('Syllabus review')} · {semester}", 9, bold=True, color=_BRAND, gap=1
    )
    pdf.text_block(scope, 18, bold=True, gap=1)
    pdf.text_block(
        f"{t['found']} {tr('syllabi')} · {t['checked']} {tr('checked')} · {tr('generated')} {when}",
        9,
        color=_MUTED,
        gap=6,
    )

    # Four tiles: the key numbers.
    no_result = t["cannot_check"] + t["not_checked"]
    share = f"{round(t['with_critical'] / t['checked'] * 100)}% {tr('of checked')}" if t["checked"] else ""
    tiles = [
        (tr("Syllabi"), t["found"], f"{t['checked']} {tr('checked')}", _TILE, _MUTED, _INK),
        (tr("With critical findings"), t["with_critical"], share, _RED_SOFT, _RED_TEXT, _RED_TEXT),
        (tr("Major only"), t["major_only"], "", _AMBER_SOFT, _AMBER_TEXT, _AMBER_TEXT),
        (tr("No AI result yet"), no_result, "", _TILE, _MUTED, _INK),
    ]
    gap, y = 4, pdf.get_y()
    w = (pdf.width - gap * 3) / 4
    label_h = max(pdf.height_of(tile[0], 9, w - 8) for tile in tiles)
    tile_h = 3 + label_h + 9 + 5 + 2
    for n, (label, value, foot, fill, label_color, value_color) in enumerate(tiles):
        x = pdf.l_margin + n * (w + gap)
        pdf.set_fill_color(*fill)
        pdf.rect(x, y, w, tile_h, style="F", round_corners=True, corner_radius=2.5)
        # Labels are longer in Russian and Kazakh: they may take two lines, and the number sits under them.
        pdf.set_xy(x + 4, y + 3)
        pdf.set_font("Body", "", 9)
        pdf.set_text_color(*label_color)
        pdf.multi_cell(w - 8, 9 * _LH, label, align="L", max_line_height=9 * _LH)
        pdf.set_xy(x + 4, y + 3 + label_h)
        pdf.set_font("Body", "B", 16)
        pdf.set_text_color(*value_color)
        pdf.cell(w - 8, 8, str(value))
        if foot:
            pdf.set_xy(x + 4, y + 12 + label_h)
            pdf.set_font("Body", "", 8.5)
            pdf.set_text_color(*label_color)
            pdf.cell(w - 8, 4, _one_line(pdf, foot, w - 8))
    pdf.set_y(y + tile_h + 4)
    if t["found"]:
        pdf.bar(
            pdf.l_margin,
            pdf.get_y(),
            pdf.width,
            2.5,
            [
                (t["with_critical"], _RED),
                (t["major_only"], _AMBER),
                (t["minor_only"], _GREY),
                (t["clean"], _GREEN),
                (no_result, _LIGHT),
            ],
        )
        pdf.ln(6)

    if not report.get("school_id") and len(report["schools"]) > 1:
        pdf.heading(tr("By School"), 12)
        _table(
            pdf,
            [tr("School"), tr("Syllabi"), tr("With critical findings")],
            [0, 25, 50],
            [
                [tr(_short_school(s["school_name"])), str(s["found"]), str(s["with_critical"])]
                for s in report["schools"]
            ],
            colors=[None, None, _RED_TEXT],
            text_col=0,
        )

    issues = sorted(report["issues"], key=lambda i: (not i["critical"], -i["syllabi"]))[:10]
    if issues:
        pdf.heading(tr("Most common findings"), 12)
        for i in issues:
            pdf.room(10)
            y = pdf.get_y()
            pdf.pill(pdf.l_margin, y + 0.3, i["criterion_id"], _TILE, _MUTED, 8.5)
            name_x, name_w = pdf.l_margin + 15, pdf.width - 15 - 62
            pdf.set_xy(name_x, y)
            pdf.set_font("Body", "", 9.5)
            pdf.set_text_color(*_INK)
            pdf.multi_cell(name_w, 9.5 * _LH, tr(i["name"]), align="L", new_x=XPos.LMARGIN, new_y=YPos.NEXT)
            end = pdf.get_y()
            if i["critical"]:
                pdf.pill(name_x, end + 0.3, tr("critical"), _RED_SOFT, _RED_TEXT, 7.5)
                end += 5
            bar_x = pdf.l_margin + pdf.width - 60
            pdf.bar(
                bar_x,
                y + 1.6,
                32,
                2.4,
                [(i["percent"], _RED if i["critical"] else _AMBER), (100 - i["percent"], _LINE)],
            )
            pdf.set_xy(bar_x + 34, y)
            pdf.set_font("Body", "B", 9.5)
            pdf.set_text_color(*_INK)
            pdf.cell(12, 9.5 * _LH, f"{i['percent']}%", align="R")
            pdf.set_font("Body", "", 9)
            pdf.set_text_color(*_MUTED)
            pdf.cell(0, 9.5 * _LH, f"· {i['syllabi']}", align="R")
            pdf.set_y(end + 2)
            pdf.set_draw_color(*_LINE)
            pdf.line(pdf.l_margin, pdf.get_y(), pdf.w - pdf.r_margin, pdf.get_y())
            pdf.ln(2)

    courses = report["courses"]
    if courses:
        title = tr("Courses with critical findings" if report["include"] == "critical" else "Courses")
        pdf.heading(f"{title} · {len(courses)}", 12)
        _table(
            pdf,
            ["#", tr("Course"), tr("Critical"), tr("Major"), tr("Minor")],
            [12, 0, 20, 18, 18],
            [
                [
                    str(n),
                    c["course_name"],
                    str(c["critical"] or ""),
                    str(c["major"] or ""),
                    str(c["minor"] or ""),
                ]
                for n, c in enumerate(courses, 1)
            ],
            colors=[_MUTED, None, _RED_TEXT, _AMBER_TEXT, _MUTED],
        )

    pdf.ln(5)
    text = tr(
        "Findings are suggestions from the AI check for the professor to review, not an approval decision."
    )
    h = pdf.height_of(text, 9, pdf.width - 10) + 7
    pdf.room(h)
    pdf.set_fill_color(*_TILE)
    pdf.rect(pdf.l_margin, pdf.get_y(), pdf.width, h, style="F", round_corners=True, corner_radius=2.5)
    pdf.set_y(pdf.get_y() + 3.5)
    pdf.text_block(text, 9, color=_MUTED, x=pdf.l_margin + 5, w=pdf.width - 10, gap=4)


def _table(
    pdf: _Pdf, head: list[str], widths: list[float], rows: list[list[str]], colors: list, text_col: int = 1
) -> None:
    """A plain table: a header, then rows with a hairline between them and every other row shaded.
    A width of 0 takes what the others leave; numbers sit to the right."""
    rest = pdf.width - sum(widths)
    widths = [w or rest for w in widths]
    numeric = [n != text_col for n in range(len(head))]
    wrap = text_col  # the one text column, which may take several lines
    size, lh = 9.5, 9.5 * _LH

    def header() -> None:
        pdf.set_font("Body", "B", 8.5)
        pdf.set_text_color(*_MUTED)
        for h, w, num in zip(head, widths, numeric, strict=True):
            if num:
                pdf.cell(w, 7, h, align="R")
            else:
                pdf.cell(3, 7, "")
                pdf.cell(w - 3, 7, h)
        pdf.ln(7)
        pdf.set_draw_color(200, 204, 209)
        pdf.set_line_width(0.5)
        pdf.line(pdf.l_margin, pdf.get_y(), pdf.w - pdf.r_margin, pdf.get_y())
        pdf.set_line_width(0.2)

    header()
    for k, row in enumerate(rows):
        h = max(pdf.height_of(row[wrap], size, widths[wrap] - 5), lh) + 3
        if pdf.get_y() + h > pdf.page_break_trigger:
            pdf.add_page()
            header()
        y = pdf.get_y()
        if k % 2:
            pdf.set_fill_color(250, 251, 252)
            pdf.rect(pdf.l_margin, y, pdf.width, h, style="F")
        x = pdf.l_margin
        for n, (cell, w) in enumerate(zip(row, widths, strict=True)):
            pdf.set_xy(x, y + 1.5)
            pdf.set_font("Body", "B" if colors[n] in (_RED_TEXT,) and cell else "", size)
            pdf.set_text_color(*(colors[n] or _INK))
            if numeric[n]:
                pdf.cell(w, lh, cell, align="R")
            else:
                pdf.set_x(x + 3)
                pdf.multi_cell(w - 5, lh, cell, align="L")
            x += w
        pdf.set_y(y + h)
        pdf.set_draw_color(*_LINE)
        pdf.line(pdf.l_margin, pdf.get_y(), pdf.w - pdf.r_margin, pdf.get_y())
    pdf.ln(3)


def _one_line(pdf: _Pdf, text: str, w: float) -> str:
    """`text` cut with an ellipsis so it fits one line `w` wide in the current font."""
    if pdf.get_string_width(text) <= w:
        return text
    while text and pdf.get_string_width(text + "…") > w:
        text = text[:-1]
    return text.rstrip() + "…"


# Type sizes on a course page, in points: compact, the way a printed report reads.
_TITLE, _META, _HEAD, _NAME, _BODY, _ROW, _SMALL = 14, 8.5, 11, 10, 9.5, 9, 8.5


def _course(pdf: _Pdf, c: dict) -> None:
    """One course on one page, or on two when it has a lot to say. Critical findings get what is wrong in
    this syllabus and how to fix it; major ones a line each; minor ones a short list. Only a course that
    would run past two pages has its critical explanations shortened, and past that its major list cut."""
    tr = pdf.tr
    pdf.add_page()
    pdf.text_block(c["course_name"], _TITLE, bold=True, gap=1)
    school = _short_school(c["school_name"])
    program = (
        c["program_name"]
        if c["program_name"] and c["program_name"] not in (c["school_name"], school)
        else None
    )
    language = tr(_LANGUAGE_NAMES.get(c["language"], c["language"])) if c["language"] else None
    meta = " · ".join(
        x for x in [c["professor_name"] or tr("No professor matched"), language, tr(school), program] if x
    )
    pdf.text_block(meta, _META, color=_MUTED, gap=1.5)
    x, y = pdf.l_margin, pdf.get_y()
    for sev, fill, color in (
        ("critical", _RED_SOFT, _RED_TEXT),
        ("major", _AMBER_SOFT, _AMBER_TEXT),
        ("minor", _TILE, _MUTED),
    ):
        if c[sev]:
            x += pdf.pill(x, y, f"{c[sev]} {tr(sev)}", fill, color, _SMALL) + 2
    pdf.set_y(y + 6)

    crit = [f for f in c["findings"] if f["severity"] == "critical"]
    major = [f for f in c["findings"] if f["severity"] == "major"]
    minor = [f for f in c["findings"] if f["severity"] == "minor"]
    left, inner = pdf.l_margin + 4, pdf.width - 4
    head_h, row_h = 3 + _HEAD * _LH + 1.5 + 2.5 + 0.5, _ROW * _LH + 1.2
    fix_label = f"**{tr('How to fix')}:**"

    def texts(f: dict, clip: int | None) -> tuple[str, str]:
        e = _finding_texts(f, pdf.lang)
        why = e["explanation"] or e["problem"]
        why = why if clip is None else (_clip(why, clip) if clip else "")
        return why, e["fix"] or _clip(e["recommendation"], 240)

    def crit_item(f: dict, clip: int | None) -> float:
        why, fix = texts(f, clip)
        h = pdf.height_of(tr(f["name"]), _NAME, inner - 12, bold=True) + 0.8
        if why:
            h += pdf.height_of(why, _BODY, inner) + 0.8
        if fix:
            h += pdf.height_of(f"{fix_label} {fix}", _BODY, inner - 5, markdown=True) + 3
        return h + 3

    def major_line(f: dict) -> str:
        return tr(f["name"])

    minor_text = " · ".join(f"{f['criterion_id']} {tr(f['name'])}" for f in minor)
    minor_h = (head_h + pdf.height_of(minor_text, _SMALL, pdf.width) + 1) if minor else 0
    major_h = (head_h + len(major) * row_h + 1) if major else 0

    def total(clip: int | None) -> float:
        return (head_h + sum(crit_item(f, clip) for f in crit) if crit else 0) + major_h + minor_h

    # One page if it fits; else two, with whole findings moved to the second page (allow for that gap).
    bottom = pdf.page_break_trigger - 4
    first, second = bottom - pdf.get_y(), bottom - pdf.t_margin
    room = first + second - 25
    clip: int | None = None
    if total(None) > first:
        for clip in (None, 320, 220, 150, 100, 0):
            if total(clip) <= room:
                break
    rows = len(major)
    while major and rows > 1 and total(clip) - major_h + head_h + rows * row_h + 1 > room:
        rows -= 1
    shown_major = major if rows >= len(major) else major[: rows - 1]

    def fit(h: float) -> None:
        if pdf.get_y() + h > bottom:
            pdf.add_page()

    def section(title: str, color, rule) -> None:
        fit(head_h + 8)
        pdf.ln(3)
        pdf.text_block(title, _HEAD, bold=True, color=color, gap=1.5)
        pdf.set_draw_color(*rule)
        pdf.set_line_width(0.5)
        pdf.line(pdf.l_margin, pdf.get_y(), pdf.w - pdf.r_margin, pdf.get_y())
        pdf.set_line_width(0.2)
        pdf.ln(2.5)

    if crit:
        section(f"{tr('Critical')} · {len(crit)}", _RED_TEXT, _RED)
        for f in crit:
            why, fix = texts(f, clip)
            fit(crit_item(f, clip))  # a finding is never split across the two pages
            top = pdf.get_y()
            w = pdf.pill(left, top + 0.3, f["criterion_id"], _TILE, _MUTED, _SMALL)
            pdf.set_xy(left + w + 2, top)
            pdf.set_font("Body", "B", _NAME)
            pdf.set_text_color(*_INK)
            pdf.multi_cell(
                inner - w - 2, _NAME * _LH, tr(f["name"]), align="L", new_x=XPos.LMARGIN, new_y=YPos.NEXT
            )
            pdf.ln(0.8)
            if why:
                pdf.text_block(why, _BODY, color=(60, 60, 60), x=left, w=inner, gap=0.8)
            if fix:
                fh = pdf.height_of(f"{fix_label} {fix}", _BODY, inner - 5, markdown=True) + 2.4
                fy = pdf.get_y()
                pdf.set_fill_color(*_FIX_BG)
                pdf.rect(left, fy, inner, fh, style="F", round_corners=True, corner_radius=1.5)
                pdf.set_xy(left + 2.5, fy + 1.2)
                pdf.set_text_color(*_FIX_TEXT)
                pdf.set_font("Body", "", _BODY)
                pdf.multi_cell(
                    inner - 5,
                    _BODY * _LH,
                    f"{fix_label} {fix}",
                    align="L",
                    markdown=True,
                    new_x=XPos.LMARGIN,
                    new_y=YPos.NEXT,
                )
                pdf.set_y(fy + fh)
            pdf.set_fill_color(*_RED)
            pdf.rect(pdf.l_margin, top, 1.2, pdf.get_y() - top, style="F")
            pdf.ln(3)

    if major:
        section(f"{tr('Major')} · {len(major)}", _AMBER_TEXT, _AMBER)
        items = [(f["criterion_id"], major_line(f)) for f in shown_major]
        if len(shown_major) < len(major):
            items.append(("", f"+{len(major) - len(shown_major)} {tr('more in Syllabus AI')}"))
        for cid, line in items:
            fit(row_h)
            pdf.set_x(pdf.l_margin)
            pdf.set_font("Body", "B", _SMALL)
            pdf.set_text_color(*_AMBER_TEXT)
            pdf.cell(10, row_h, cid)
            pdf.set_font("Body", "", _ROW)
            pdf.set_text_color(*(_INK if cid else _MUTED))
            pdf.cell(
                pdf.width - 10,
                row_h,
                _one_line(pdf, line, pdf.width - 11),
                new_x=XPos.LMARGIN,
                new_y=YPos.NEXT,
            )

    if minor:
        section(f"{tr('Minor')} · {len(minor)}", _MUTED, _GREY)
        fit(pdf.height_of(minor_text, _SMALL, pdf.width))
        pdf.text_block(minor_text, _SMALL, color=_MUTED, gap=0)


def render_pdf(report: dict, lang: Lang = "en") -> bytes:
    """Set the report in a PDF: a summary page, then each course on a page of its own, its findings as cards."""
    tr = translator(lang)
    scope = tr(_short_school(report["school_name"]) if report.get("school_name") else "Whole university")
    semester = _semester(report["semester"], tr)
    pdf = _Pdf(
        f"{tr('Narxoz University')} · {tr('Syllabus review')} · {semester} · {scope}",
        lang,
        backup_font=_has_cjk(report),
    )
    pdf.add_page()
    _summary(pdf, report, scope)
    for c in report["courses"]:
        _course(pdf, c)
    return bytes(pdf.output())


def filename(report: dict) -> str:
    scope = _short_school(report["school_name"]) if report.get("school_name") else "University"
    slug = "-".join(f"{scope} {report['semester']}".split())
    return f"Syllabus-review-{slug}.pdf"


async def build(
    session: AsyncSession,
    principal: Principal,
    *,
    semester: str,
    school_id: uuid.UUID | None,
    include: Include,
    lang: Lang = "en",
) -> tuple[dict, bytes, str]:
    report = await analysis.report(
        session, principal, semester=semester, school_id=school_id, include=include
    )
    pdf = await asyncio.to_thread(render_pdf, report, lang)
    return report, pdf, filename(report)


async def _recipients(session: AsyncSession) -> list[str]:
    configured = [a.strip() for a in get_settings().provost_office_email.split(",") if a.strip()]
    if configured:
        return configured
    rows = await session.scalars(
        sa.select(User.email)
        .join(RoleAssignment, RoleAssignment.user_id == User.id)
        .where(RoleAssignment.role == Role.provost, User.is_active.is_(True))
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
    session: AsyncSession,
    principal: Principal,
    *,
    semester: str,
    school_id: uuid.UUID | None,
    include: Include,
    lang: Lang = "en",
) -> ReviewReport:
    """Make the PDF and mail it to the Provost Office, then record that it went."""
    to = await _recipients(session)
    if not to:
        raise NoRecipient("Nobody to send it to: set PROVOST_OFFICE_EMAIL, or give someone the provost role.")
    report, pdf, name = await build(
        session, principal, semester=semester, school_id=school_id, include=include, lang=lang
    )
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
        log.info(
            "EMAIL to=%s subject=%s attachment=%s (%d bytes, kept in %s)\n%s",
            ", ".join(to),
            subject,
            name,
            len(pdf),
            outbox / name,
            body,
        )
    else:
        await asyncio.to_thread(_send_smtp, to, subject, body, pdf, name)
    row = ReviewReport(
        semester=semester,
        school_id=school_id,
        include=include,
        courses=len(report["courses"]),
        recipients=to,
        filename=name,
        sent_by=principal.id,
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
        query = query.where(
            sa.or_(ReviewReport.school_id.in_(principal.school_ids), ReviewReport.school_id.in_(heads))
        )
    return [
        {
            "id": r.id,
            "semester": r.semester,
            "school_id": r.school_id,
            "school_name": school,
            "include": r.include,
            "courses": r.courses,
            "recipients": r.recipients,
            "filename": r.filename,
            "sent_by": who,
            "sent_at": r.created_at,
        }
        for r, who, school in (await session.execute(query)).all()
    ]
