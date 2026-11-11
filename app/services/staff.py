"""Client for the external Staff / HR API (GET /api/v1/staff/external/employees).

Returns the list of active university employees for user provisioning.
Caches responses in Redis to keep the admin panel responsive and reduce load on the HR service.
"""

import logging
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


def detect_school(department: str | None) -> str | None:
    if not department:
        return None
    d = department.strip().lower()
    # 1. Exact or substring match against canonical school names
    for name in SCHOOL_NAMES_ORDER:
        if name.lower() in d or d in name.lower():
            return name
    # 2. Acronyms / short codes
    if "шэм" in d:
        return "Школа экономики и менеджмента"
    if "шцт" in d:
        return "Школа цифровых технологий"
    if "шспн" in d:
        return "Школа социальных и прикладных наук"
    if "шпигу" in d:
        return "Школа права и государственного управления"
    if "шбнп" in d:
        return "Школа базовых и междисциплинарных программ"
    # 3. Department keywords
    if any(k in d for k in ("экономик", "финанс", "учет", "аудит", "маркетинг", "менеджмент", "бизнес")):
        return "Школа экономики и менеджмента"
    if any(k in d for k in ("цифров", "информацион", "программ", "компьютер", "технолог", "it")):
        return "Школа цифровых технологий"
    if any(k in d for k in ("социальн", "прикладн", "язык", "гуманитарн", "истори", "философ", "психолог")):
        return "Школа социальных и прикладных наук"
    if any(k in d for k in ("прав", "юриспруд", "государствен", "юридическ")):
        return "Школа права и государственного управления"
    if any(k in d for k in ("базов", "междисциплин", "общеобразовательн")):
        return "Школа базовых и междисциплинарных программ"
    return None


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
    url = f"{settings.staff_api_base_url.rstrip('/')}/api/v1/staff/external/employees"
    headers = {}
    if settings.staff_api_token:
        headers["Authorization"] = f"Bearer {settings.staff_api_token}"

    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await client.get(url, headers=headers)
            response.raise_for_status()
            data = response.json()
    except Exception as exc:
        log.warning("failed to fetch employees from %s: %s", url, exc)
        # If cache expired but exists or fallback needed, return empty list or fallback
        return (
            [StaffEmployee.model_validate(item) for item in MOCK_EMPLOYEES] if settings.env != "prod" else []
        )

    if not isinstance(data, list):
        log.error("unexpected staff API response format: expected list, got %s", type(data))
        return []

    employees = []
    for raw in data:
        try:
            dept = str(raw.get("department") or "").strip() or None if raw.get("department") else None
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
