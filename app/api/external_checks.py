"""Store a check a person's own AI did (through the MCP server), see app/services/external_checks.py."""

import uuid

from fastapi import APIRouter, status

from app.deps import CurrentUser, DbSession
from app.schemas.external_check import ExternalCheckIn, ExternalCheckOut
from app.services import external_checks, syllabi

router = APIRouter(tags=["external-checks"])


@router.post(
    "/versions/{version_id}/external-check",
    response_model=ExternalCheckOut,
    status_code=status.HTTP_201_CREATED,
    summary="Save a check an outside AI made of this syllabus version",
)
async def submit_external_check(
    version_id: uuid.UUID, body: ExternalCheckIn, principal: CurrentUser, session: DbSession
):
    """Saves the result of judging a syllabus version against the rubric with an AI of your own (Claude, ChatGPT):
    one answer for EVERY criterion of the active rubric (get_rubric), each with a quote from the syllabus text
    (get_syllabus_text). A quote that is not in the text turns that criterion into 'Cannot verify'. The check is stored
    as a completed run marked 'External AI' with the model's name, and the analysis and the review report then count it.
    Costs nothing. For the Provost Office, KOK and the School's administrators."""
    version = await syllabi.get_version(session, version_id)
    return await external_checks.submit(session, principal, version, body)
