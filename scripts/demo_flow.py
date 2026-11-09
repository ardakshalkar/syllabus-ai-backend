"""End-to-end demo route of the prototype README against a running stack (API + Celery worker).

    docker compose up -d && docker compose exec api python -m app.cli seed
    uv run python scripts/demo_flow.py [--base http://localhost:8080/api/v1]

Session check → results → suggest corrections → v(N+1) → Head of Program → KOK → Approved.
"""

import argparse
import sys
import time

import httpx

SEMESTER = "Fall 2026"


def login(base: str, email: str) -> httpx.Client:
    return httpx.Client(base_url=base, timeout=120, headers={"Authorization": f"Bearer stub:{email}"})


def wait(fetch, done, what: str, timeout: float = 600):
    started = time.monotonic()
    while True:
        value = fetch()
        if done(value):
            return value
        if time.monotonic() - started > timeout:
            sys.exit(f"timed out waiting for {what}")
        time.sleep(1)


def step(text: str) -> None:
    print(f"\n== {text}")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base", default="http://localhost:8080/api/v1")
    base = parser.parse_args().base

    provost = login(base, "provost@demo.narxoz.kz")
    lead = login(base, "shalkar@demo.narxoz.kz")
    head = login(base, "head.cs@demo.narxoz.kz")
    kok = login(base, "kok@demo.narxoz.kz")

    step("1. Provost starts a university-wide check")
    session = (
        provost.post(
            "/sessions",
            json={
                "name": f"{SEMESTER} University Syllabus Review",
                "scope_type": "university",
                "semester": SEMESTER,
            },
        )
        .raise_for_status()
        .json()
    )
    session = wait(
        lambda: provost.get(f"/sessions/{session['id']}").json(),
        lambda s: s["status"] == "Completed",
        "session",
    )
    print(f"session {session['status']}: {session['progress']}")
    for row in provost.get("/overview", params={"semester": SEMESTER}).json():
        print(
            f"  {row['school_name']}: found {row['found']}, checked {row['checked']}, "
            f"needs revision {row['needs_revision']}"
        )

    step("2. Professor opens Artificial Intelligence results")
    syllabus = next(
        s
        for s in lead.get("/syllabi", params={"mine": True}).json()["items"]
        if s["course_name"] == "Artificial Intelligence"
    )
    detail = lead.get(f"/syllabi/{syllabus['id']}").json()
    run = lead.get(f"/runs/{detail['latest_run_id']}").json()
    print(f"v{detail['current_version']} · {run['issues']} issues · stage {detail['approval_stage']}")
    for r in run["results"]:
        if r["status"] != "Compliant":
            print(f"  {r['criterion_id']} {r['status']}: {r['explanation'][:90]}")

    step("3. Suggest corrections, accept them, create the next version")
    proposals = lead.post(f"/runs/{run['id']}/proposals").raise_for_status().json()
    for item in proposals["items"]:
        lead.patch(
            f"/proposals/{proposals['id']}/items/{item['id']}", json={"decision": "accepted"}
        ).raise_for_status()
        print(f"  accepted {item['criterion_id']}: {item['title']}")
    applied = lead.post(f"/proposals/{proposals['id']}/apply").raise_for_status().json()
    new_run = wait(
        lambda: lead.get(f"/runs/{applied['run']['id']}").json(),
        lambda r: r["status"] in ("Completed", "Failed"),
        "re-check",
    )
    print(f"v{applied['version']['version']} checked: {new_run['issues']} issues")

    step("4. Academic approval")
    sid = syllabus["id"]
    for client, stage, comment in (
        (lead, "Head of Program Review", None),
        (head, "KOK Review", "Outcomes and assessment are now aligned."),
        (kok, "Approved", None),
    ):
        client.post(
            f"/syllabi/{sid}/transitions", json={"to_stage": stage, "comment": comment}
        ).raise_for_status()
        print(f"  → {stage}")
    final = lead.get(f"/syllabi/{sid}").json()
    versions = lead.get(f"/syllabi/{sid}/versions").json()
    print(f"final stage: {final['approval_stage']}; versions kept: {[v['version'] for v in versions]}")
    assert final["approval_stage"] == "Approved"


if __name__ == "__main__":
    main()
