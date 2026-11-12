from fastapi import APIRouter

from app.api import (
    admin,
    analytics,
    approval,
    auth,
    catalog,
    drive,
    improve,
    me,
    notifications,
    precheck,
    semesters,
    sessions,
    syllabi,
    wizard,
)

api_router = APIRouter()
for module in (
    admin,
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
    semesters,
):
    api_router.include_router(module.router)
