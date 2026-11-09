from fastapi import APIRouter

from app.api import (
    analytics,
    approval,
    catalog,
    drive,
    improve,
    me,
    notifications,
    precheck,
    sessions,
    syllabi,
    wizard,
)

api_router = APIRouter()
for module in (
    me,
    catalog,
    drive,
    syllabi,
    approval,
    improve,
    precheck,
    wizard,
    sessions,
    analytics,
    notifications,
):
    api_router.include_router(module.router)
