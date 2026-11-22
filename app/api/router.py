from fastapi import APIRouter

from app.api import (
    admin,
    alignment,
    analytics,
    approval,
    auth,
    catalog,
    drive,
    improve,
    me,
    notifications,
    oauth,
    precheck,
    semesters,
    sessions,
    study_plans,
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
    alignment,
    approval,
    improve,
    precheck,
    wizard,
    sessions,
    analytics,
    notifications,
    oauth,
    semesters,
    study_plans,
):
    api_router.include_router(module.router)
