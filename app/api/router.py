from fastapi import APIRouter

from app.api import (
    analytics,
    approval,
    auth,
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
    auth,
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
