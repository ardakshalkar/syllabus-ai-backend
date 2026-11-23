"""The text of a syllabus version, for a person's own AI to judge it (through the MCP server)."""

import uuid

from app.documents import ExtractedDocument
from seed.samples import sample_syllabus
from tests.conftest import create_syllabus, upload


async def test_the_text_of_a_version_goes_to_those_who_may_see_it(ref, client_for):
    lead = client_for(ref["users"]["shalkar"])
    syllabus = await create_syllabus(lead, ref["courses"]["ai"].id)
    uploaded = await upload(
        lead, syllabus["id"], sample_syllabus("Artificial Intelligence", with_issues=False)
    )
    version_id = uploaded["version"]["id"]

    mine = await lead.get(f"/versions/{version_id}/text")
    assert mine.status_code == 200, mine.text
    body = mine.json()
    assert "Artificial Intelligence" in body["text"] and body["chars"] == len(body["text"])
    assert body["version_id"] == version_id and body["filename"].endswith(".docx")

    assert (await client_for(ref["users"]["provost"]).get(f"/versions/{version_id}/text")).status_code == 200
    stranger = client_for(ref["users"]["sultanov"])  # teaches another course
    assert (await stranger.get(f"/versions/{version_id}/text")).status_code == 404  # not even told it exists


async def test_a_version_not_read_yet_has_no_text(ref, client_for):
    lead = client_for(ref["users"]["shalkar"])
    syllabus = await create_syllabus(lead, ref["courses"]["ai"].id)
    uploaded = await upload(
        lead, syllabus["id"], sample_syllabus("Artificial Intelligence", with_issues=False)
    )
    version_id = uploaded["version"]["id"]
    doc = await ExtractedDocument.find_one(ExtractedDocument.version_id == uuid.UUID(version_id))
    doc.content.raw_text = ""
    await doc.save()
    missing = await lead.get(f"/versions/{version_id}/text")
    assert missing.status_code == 404 and "no text" in missing.json()["detail"]
