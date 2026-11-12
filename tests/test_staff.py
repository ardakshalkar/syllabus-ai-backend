"""Which School an employee from the staff API belongs to (app/services/staff.py)."""

import pytest

from app.services.staff import SCHOOL_NAMES_ORDER, detect_school

ECON, DIGITAL, SOCIAL, LAW, BASIC = SCHOOL_NAMES_ORDER


@pytest.mark.parametrize(
    ("department", "school"),
    [
        ("Школа экономики и менеджмента", ECON),
        ("Школа цифровых технологий", DIGITAL),
        ("Школа социальных и прикладных наук", SOCIAL),
        ("Школа права и государственного управления", LAW),
        ("Школа базовых и междисциплинарных программ", BASIC),
        ("(АУП) Школа цифровых технологий", DIGITAL),
        ("(АУП) Школа базовых и междисциплинарных програм", BASIC),  # typo as it comes from HR
        ("  школа ПРАВА и государственного управления ", LAW),
        ("Кафедра финансов · ШЭМ", ECON),
        ("ШПиГУ", LAW),
    ],
)
def test_a_school_is_found_by_its_name_or_code(department, school):
    assert detect_school(department) == school


@pytest.mark.parametrize(
    "department",
    [
        None,
        "",
        "Отдел по управлению имуществом",
        "Департамент информационных технологий",
        "Департамент маркетинга и приема",
        "Отдел бухгалтерского и налогового учета",
        "Правление",
        "Юридический отдел",
        "Центр междисциплинарного образования",
        "Бизнес-школа",
        "Военная кафедра",
    ],
)
def test_offices_and_other_units_get_no_school(department):
    assert detect_school(department) is None
