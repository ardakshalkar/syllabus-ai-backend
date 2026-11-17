import uuid
from datetime import datetime, timezone
import pytest
from app.services import review_report


def test_fonts_finds_valid_pair():
    reg, bold = review_report._fonts()
    assert reg.is_file()
    assert bold.is_file()


def test_render_pdf_multilingual():
    sample_report = {
        "semester": "Fall 2026",
        "school_id": None,
        "school_name": None,
        "schools": [
            {"school_id": uuid.uuid4(), "school_name": "School of Economics and Management", "found": 10, "with_critical": 2}
        ],
        "totals": {
            "found": 10,
            "checked": 8,
            "with_critical": 2,
            "major_only": 3,
            "minor_only": 1,
            "clean": 2,
            "cannot_check": 0,
            "not_checked": 2,
        },
        "issues": [
            {
                "criterion_id": "3.1",
                "name": "Course learning outcomes",
                "principle": 3,
                "principle_name": "Goals and outcomes",
                "critical": True,
                "syllabi": 2,
                "percent": 25,
            }
        ],
        "generated_at": datetime.now(timezone.utc),
        "include": "all",
        "courses": [
            {
                "syllabus_id": uuid.uuid4(),
                "course_name": "Микроэкономика",
                "professor_name": "Ахметов А.А.",
                "language": "Russian",
                "school_name": "School of Economics and Management",
                "program_name": "Экономика",
                "critical": 1,
                "major": 0,
                "minor": 0,
                "findings": [
                    {
                        "criterion_id": "3.1",
                        "name": "Course learning outcomes",
                        "principle": 3,
                        "principle_name": "Цели и результаты обучения",
                        "severity": "critical",
                        "evidence": "Цитата из силлабуса",
                        "explanation": "Default explanation in English",
                        "recommendation": "Default fix in English",
                        "texts": {
                            "problem": {"ru": "Результаты обучения не согласованы", "kk": "Оқыту нәтижелері сәйкес емес"},
                            "fix": {"ru": "Согласуйте РО с темами", "kk": "ОН тақырыптармен сәйкестендіріңіз"},
                        },
                        "translations": {
                            "ru": {"explanation": "Объяснение на русском", "recommendation": "Рекомендация на русском"},
                            "kk": {"explanation": "Қазақша түсіндірме", "recommendation": "Қазақша ұсыныс"},
                        },
                    }
                ],
            }
        ],
    }

    # Test Russian rendering
    pdf_ru = review_report.render_pdf(sample_report, lang="ru")
    assert isinstance(pdf_ru, bytes)
    assert len(pdf_ru) > 1000

    # Test Kazakh rendering
    pdf_kk = review_report.render_pdf(sample_report, lang="kk")
    assert isinstance(pdf_kk, bytes)
    assert len(pdf_kk) > 1000

    # Test Single syllabus rendering
    sample_report["single_syllabus"] = True
    pdf_single = review_report.render_pdf(sample_report, lang="ru")
    assert isinstance(pdf_single, bytes)
    assert len(pdf_single) > 500
