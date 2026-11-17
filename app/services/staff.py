"""Client for the external Staff / HR API (GET /api/v1/staff/external/employees).

Returns the list of active university employees for user provisioning.
Caches responses in Redis to keep the admin panel responsive and reduce load on the HR service.
"""

import logging
import re
from typing import Any

import httpx
from pydantic import BaseModel, Field

from app.config import get_settings
from app.storage import cache

log = logging.getLogger(__name__)

CACHE_KEY = "staff:external_employees"

SCHOOL_NAMES_ORDER = [
    "Школа экономики и менеджмента",
    "Школа цифровых технологий",
    "Школа социальных и прикладных наук",
    "Школа права и государственного управления",
    "Школа базовых и междисциплинарных программ",
]


# Short codes the HR system and people use for each School.
_SCHOOL_CODES = {
    "шэм": SCHOOL_NAMES_ORDER[0],
    "шцт": SCHOOL_NAMES_ORDER[1],
    "шспн": SCHOOL_NAMES_ORDER[2],
    "шпигу": SCHOOL_NAMES_ORDER[3],
    "шбнп": SCHOOL_NAMES_ORDER[4],
}


def _normalize(text: str) -> str:
    """Lower-case, drop "(АУП)" and other bracketed notes, fold ё, squeeze spaces and doubled letters.

    Folding doubled letters lets "програм" (a typo in the HR data) match "программ".
    """
    t = re.sub(r"\([^)]*\)", " ", text.lower()).replace("ё", "е")
    t = re.sub(r"[^\w\s]", " ", t)
    t = re.sub(r"(\w)\1+", r"\1", t)
    return " ".join(t.split())


_SCHOOLS_NORMALIZED = [(_normalize(name), name) for name in SCHOOL_NAMES_ORDER]


def detect_school(department: str | None) -> str | None:
    """The School a department belongs to, or None.

    Matches only a School's full name or its short code. Guessing by topic words is
    deliberately left out: it put offices such as "Отдел по управлению имуществом"
    into the School of Law and "Департамент информационных технологий" into Digital.
    """
    if not department:
        return None
    d = _normalize(department)
    for normalized, name in _SCHOOLS_NORMALIZED:
        if normalized in d:
            return name
    words = set(d.split())
    for code, name in _SCHOOL_CODES.items():
        if code in words:
            return name
    return None


class StaffApiUnavailable(RuntimeError):
    """The staff service failed or answered in an unexpected shape (raised in prod only)."""


class StaffEmployee(BaseModel):
    full_name: str = Field(description="Фамилия Имя Отчество")
    email: str = Field(description="Рабочая почта @narxoz.kz")
    department: str | None = Field(default=None, description="Основное подразделение/кафедра")
    school: str | None = Field(default=None, description="Определенная школа")


# Fallback employees used in development / testing when STAFF_API_BASE_URL is not configured.
MOCK_EMPLOYEES: list[dict[str, Any]] = [
    {
        "full_name": "Сидоров Петр Алексеевич",
        "email": "sidorov@narxoz.kz",
        "department": "Школа экономики и менеджмента",
        "school": "Школа экономики и менеджмента",
    },
    {
        "full_name": "Ахметова Динара Болатовна",
        "email": "akhmetova@narxoz.kz",
        "department": "Кафедра финансов и учета · Школа экономики и менеджмента",
        "school": "Школа экономики и менеджмента",
    },
    {
        "full_name": "Иванов Иван Иванович",
        "email": "ivanov@narxoz.kz",
        "department": "Школа цифровых технологий",
        "school": "Школа цифровых технологий",
    },
    {
        "full_name": "Жумабеков Азамат Кайратович",
        "email": "zhumabekov@narxoz.kz",
        "department": "Кафедра IT и цифровых систем",
        "school": "Школа цифровых технологий",
    },
    {
        "full_name": "Касымов Нурлан Маратович",
        "email": "kassymov@narxoz.kz",
        "department": "Школа социальных и прикладных наук",
        "school": "Школа социальных и прикладных наук",
    },
    {
        "full_name": "Петрова Анна Сергеевна",
        "email": "petrova@narxoz.kz",
        "department": "Школа права и государственного управления",
        "school": "Школа права и государственного управления",
    },
    {
        "full_name": "Байжанов Мурат Серикович",
        "email": "baizhanov@narxoz.kz",
        "department": "Школа базовых и междисциплинарных программ",
        "school": "Школа базовых и междисциплинарных программ",
    },
]


async def fetch_external_employees() -> list[StaffEmployee]:
    """Fetch active employees from the external staff service, with Redis caching."""
    # 1. Check Redis cache
    cached = await cache.get_json(CACHE_KEY)
    if cached is not None and isinstance(cached, list):
        try:
            return [StaffEmployee.model_validate(item) for item in cached]
        except Exception:
            log.warning("invalid staff employee data in cache, refreshing")

    settings = get_settings()

    # 2. In dev/test without configured staff API, return fallback demo list
    if not settings.staff_api_base_url:
        log.info("STAFF_API_BASE_URL is not set; using mock staff employees")
        result = [StaffEmployee.model_validate(item) for item in MOCK_EMPLOYEES]
        await cache.put_json(
            CACHE_KEY, [item.model_dump() for item in result], settings.staff_api_cache_ttl_s
        )
        return result

    # 3. Call the external endpoint
    base = settings.staff_api_base_url.rstrip("/")
    if base.endswith("/api/v1/staff/external/employees"):
        url = base
    else:
        url = f"{base}/api/v1/staff/external/employees"
    headers = {}
    if settings.staff_api_token:
        headers["Authorization"] = f"Bearer {settings.staff_api_token}"

    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await client.get(url, headers=headers)
            response.raise_for_status()
            data = response.json()
    except (httpx.HTTPError, ValueError) as exc:
        log.warning("failed to fetch employees from %s: %s", url, exc)
        if settings.env == "prod":
            raise StaffApiUnavailable(f"the staff service did not answer: {exc}") from exc
        log.warning("using mock staff employees instead (ENV=%s)", settings.env)
        return [StaffEmployee.model_validate(item) for item in MOCK_EMPLOYEES]

    if not isinstance(data, list):
        log.error("unexpected staff API response format: expected list, got %s", type(data))
        raise StaffApiUnavailable("the staff service sent something other than a list of employees")

    employees = []
    for raw in data:
        try:
            dept = str(raw.get("department") or "").strip() or None
            school = raw.get("school") or detect_school(dept)
            employees.append(
                StaffEmployee(
                    full_name=str(raw.get("full_name") or "").strip(),
                    email=str(raw.get("email") or "").strip().lower(),
                    department=dept,
                    school=school,
                )
            )
        except Exception as item_err:
            log.warning("ignoring invalid employee record %r: %s", raw, item_err)

    # Cache in Redis
    await cache.put_json(
        CACHE_KEY,
        [emp.model_dump() for emp in employees],
        settings.staff_api_cache_ttl_s,
    )
    return employees
