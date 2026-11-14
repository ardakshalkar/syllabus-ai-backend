"""Tables nested in table cells (the Narxoz template's weekly plan and assessment) reach the extracted text."""

from pathlib import Path

import docx

from app.ai.apply import Edit, apply_docx
from app.ai.extraction import extract


def _template(path: Path) -> Path:
    document = docx.Document()
    outer = document.add_table(rows=2, cols=2)
    outer.cell(0, 0).text = "Course title"
    outer.cell(0, 1).text = "Data Analysis"
    outer.cell(1, 0).text = "Assessment"
    inner = outer.cell(1, 1).add_table(rows=2, cols=2)
    inner.cell(0, 0).text = "Midterm exam"
    inner.cell(0, 1).text = "30%"
    inner.cell(1, 0).text = "Final exam"
    inner.cell(1, 1).text = "40%"
    document.save(str(path))
    return path


def test_nested_table_rows_are_extracted(tmp_path: Path) -> None:
    content = extract(_template(tmp_path / "s.docx"))
    assert "Course title | Data Analysis" in content.paragraphs
    assert "Midterm exam | 30%" in content.paragraphs
    assert "Final exam | 40%" in content.raw_text
    # the nested rows follow the outer row that holds them
    assert content.paragraphs.index("Midterm exam | 30%") > content.paragraphs.index("Assessment")


def test_proposal_applies_to_nested_row(tmp_path: Path) -> None:
    result = apply_docx(
        _template(tmp_path / "s.docx"), [Edit(current="Midterm exam | 30%", proposed="Midterm exam | 25%")]
    )
    assert result.applied == 1
