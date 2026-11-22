"""Connected apps: registering, the consent step, tokens, scopes, refresh and revoking."""

import base64
import hashlib
from urllib.parse import parse_qs, urlsplit

import httpx

from app.auth import scopes
from app.enums import Role

REDIRECT = "https://claude.ai/api/mcp/auth_callback"
VERIFIER = "v" * 64


def _challenge(verifier: str = VERIFIER) -> str:
    return base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")


def _query(url: str) -> dict[str, str]:
    return {k: v[0] for k, v in parse_qs(urlsplit(url).query).items()}


async def _register(anon: httpx.AsyncClient, redirect: str = REDIRECT) -> str:
    response = await anon.post("/oauth/register", json={"client_name": "Claude", "redirect_uris": [redirect]})
    assert response.status_code == 201, response.text
    assert response.json()["token_endpoint_auth_method"] == "none"
    return response.json()["client_id"]


async def _request(anon, client_id, scope="mcp:write", redirect=REDIRECT, state="xyz") -> str:
    response = await anon.get(
        "/oauth/authorize",
        params={
            "response_type": "code",
            "client_id": client_id,
            "redirect_uri": redirect,
            "code_challenge": _challenge(),
            "code_challenge_method": "S256",
            "scope": scope,
            "state": state,
        },
    )
    assert response.status_code == 302, response.text
    return _query(response.headers["location"])["request"]


async def _code(anon, user_client, client_id, scope="mcp:write", approve_scope=None) -> str:
    request_id = await _request(anon, client_id, scope)
    body = {"scope": approve_scope} if approve_scope else {}
    approved = await user_client.post(f"/oauth/requests/{request_id}/approve", json=body)
    assert approved.status_code == 200, approved.text
    query = _query(approved.json()["redirect_url"])
    assert query["state"] == "xyz"
    return query["code"]


async def _tokens(anon, client_id, code, verifier=VERIFIER, redirect=REDIRECT):
    return await anon.post(
        "/oauth/token",
        data={
            "grant_type": "authorization_code",
            "client_id": client_id,
            "code": code,
            "redirect_uri": redirect,
            "code_verifier": verifier,
        },
    )


async def _connect(anon, user_client, scope="mcp:write", approve_scope=None) -> dict:
    client_id = await _register(anon)
    code = await _code(anon, user_client, client_id, scope, approve_scope)
    response = await _tokens(anon, client_id, code)
    assert response.status_code == 200, response.text
    return {**response.json(), "client_id": client_id}


def _bearer(tokens: dict, base: httpx.AsyncClient) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=base._transport,
        base_url="http://test/api/v1",
        headers={"Authorization": f"Bearer {tokens['access_token']}"},
    )


async def test_metadata_says_where_everything_is(anon):
    response = await anon.get("http://test/.well-known/oauth-authorization-server")
    meta = response.json()
    assert meta["issuer"] == "http://test"
    assert meta["token_endpoint"] == "http://test/api/v1/oauth/token"
    assert meta["code_challenge_methods_supported"] == ["S256"]
    assert meta["scopes_supported"] == ["mcp:read", "mcp:write", "mcp:full"]


async def test_the_whole_flow_gives_the_user_their_own_access(ref, client_for, anon):
    provost = client_for(ref["users"]["provost"])
    tokens = await _connect(anon, provost, "mcp:read")
    assert tokens["scope"] == "mcp:read" and tokens["token_type"] == "Bearer"
    app = _bearer(tokens, anon)
    me = await app.get("/me")
    assert me.status_code == 200 and me.json()["email"] == ref["users"]["provost"].email
    assert (await app.get("/study-plans")).status_code == 200
    await app.aclose()


async def test_a_read_token_cannot_change_anything_and_a_write_token_can(ref, client_for, anon):
    provost = client_for(ref["users"]["provost"])
    plan = {"program_code": "6B06106", "courses": []}
    reader = _bearer(await _connect(anon, provost, "mcp:read"), anon)
    denied = await reader.post("/study-plans", json=plan)
    assert denied.status_code == 403 and "connected app" in denied.json()["detail"]
    writer = _bearer(await _connect(anon, provost, "mcp:write"), anon)
    created = await writer.post("/study-plans", json=plan)
    assert created.status_code == 201, created.text
    assert created.json()["status"] == "draft"  # the plan rules still apply on top
    await reader.aclose()
    await writer.aclose()


async def test_the_scope_is_decided_by_the_people_not_the_app(ref, client_for, anon):
    """The app asked for full access; a School administrator may not give it, the Provost Office may."""
    client_id = await _register(anon)
    request_id = await _request(anon, client_id, "mcp:full")
    school_admin = client_for(ref["users"]["admin.digital"])
    view = (await school_admin.get(f"/oauth/requests/{request_id}")).json()
    assert view["requested_scope"] == "mcp:full" and view["default_scope"] == "mcp:write"
    assert view["allowed_scopes"] == ["mcp:read", "mcp:write"]
    assert view["client_name"] == "Claude" and view["redirect_host"] == "claude.ai"
    refused = await school_admin.post(f"/oauth/requests/{request_id}/approve", json={"scope": "mcp:full"})
    assert refused.status_code == 403
    provost = client_for(ref["users"]["provost"])
    assert (await provost.get(f"/oauth/requests/{request_id}")).json()["allowed_scopes"][-1] == "mcp:full"
    approved = await provost.post(f"/oauth/requests/{request_id}/approve", json={"scope": "mcp:full"})
    assert approved.status_code == 200
    # a person can also give less than was asked for, but never more
    client_id = await _register(anon)
    request_id = await _request(anon, client_id, "mcp:read")
    wider = await provost.post(f"/oauth/requests/{request_id}/approve", json={"scope": "mcp:write"})
    assert wider.status_code == 400


async def test_a_connected_app_reaches_only_what_its_scope_lists(ref, client_for, anon):
    provost = client_for(ref["users"]["provost"])
    app = _bearer(await _connect(anon, provost, "mcp:full", "mcp:full"), anon)
    assert (
        await app.get("/admin/users")
    ).status_code == 403  # not on any scope, though a Provost user is no admin
    assert (await app.get("/oauth/grants")).status_code == 403  # it cannot list or widen its own connection
    created = await app.post(
        "/syllabi", json={"course_id": str(ref["courses"]["ml"].id), "semester": "Fall 2026"}
    )
    assert created.status_code == 403  # a real endpoint, but not one a connected app is given
    assert (await app.get("/study-plans")).status_code == 200
    await app.aclose()


async def test_a_wrong_verifier_a_reused_code_and_another_redirect_are_refused(ref, client_for, anon):
    provost = client_for(ref["users"]["provost"])
    client_id = await _register(anon)
    code = await _code(anon, provost, client_id)
    wrong = await _tokens(anon, client_id, code, verifier="w" * 64)
    assert wrong.status_code == 400 and wrong.json()["error"] == "invalid_grant"
    assert (await _tokens(anon, client_id, code)).status_code == 400  # the failed try used the code up
    code = await _code(anon, provost, client_id)
    assert (await _tokens(anon, client_id, code)).status_code == 200
    assert (await _tokens(anon, client_id, code)).status_code == 400  # used once
    other = await _register(anon, "https://other.example/cb")
    stolen = await _code(anon, provost, client_id)
    assert (await _tokens(anon, other, stolen)).status_code == 400  # a code belongs to its client


async def test_authorize_is_strict_about_the_client_and_where_it_sends_people(ref, anon):
    client_id = await _register(anon)
    params = {
        "response_type": "code",
        "client_id": client_id,
        "redirect_uri": REDIRECT,
        "code_challenge": _challenge(),
        "code_challenge_method": "S256",
    }
    unknown = await anon.get("/oauth/authorize", params={**params, "client_id": "nope"})
    assert unknown.status_code == 400 and unknown.json()["error"] == "invalid_client"
    elsewhere = await anon.get(
        "/oauth/authorize", params={**params, "redirect_uri": "https://evil.example/cb"}
    )
    assert elsewhere.status_code == 400  # shown as an error, never redirected there
    plain = await anon.get("/oauth/authorize", params={**params, "code_challenge_method": "plain"})
    assert plain.status_code == 302 and _query(plain.headers["location"])["error"] == "invalid_request"
    odd = await anon.get("/oauth/authorize", params={**params, "scope": "admin"})
    assert _query(odd.headers["location"])["error"] == "invalid_scope"


async def test_registration_accepts_https_and_localhost_only(anon):
    for bad in ("http://example.com/cb", "javascript:alert(1)", "https://x.example/cb#frag", "myapp://cb"):
        response = await anon.post("/oauth/register", json={"redirect_uris": [bad]})
        assert response.status_code == 400 and response.json()["error"] == "invalid_redirect_uri", bad
    assert (await anon.post("/oauth/register", json={"redirect_uris": []})).status_code == 400
    assert await _register(anon, "http://localhost:33418/callback")


async def test_a_localhost_app_may_pick_another_port(ref, client_for, anon):
    client_id = await _register(anon, "http://localhost:33418/callback")
    response = await anon.get(
        "/oauth/authorize",
        params={
            "response_type": "code",
            "client_id": client_id,
            "redirect_uri": "http://localhost:50111/callback",
            "code_challenge": _challenge(),
            "code_challenge_method": "S256",
        },
    )
    assert response.status_code == 302


async def test_refreshing_replaces_both_tokens_and_a_replayed_one_ends_the_connection(ref, client_for, anon):
    provost = client_for(ref["users"]["provost"])
    first = await _connect(anon, provost, "mcp:read")

    async def refresh(token):
        return await anon.post(
            "/oauth/token",
            data={"grant_type": "refresh_token", "client_id": first["client_id"], "refresh_token": token},
        )

    second = await refresh(first["refresh_token"])
    assert second.status_code == 200
    new = second.json()
    assert new["access_token"] != first["access_token"] and new["refresh_token"] != first["refresh_token"]
    old_app = _bearer(first, anon)
    assert (await old_app.get("/me")).status_code == 401  # the earlier access token is gone
    new_app = _bearer(new, anon)
    assert (await new_app.get("/me")).status_code == 200
    replay = await refresh(first["refresh_token"])  # someone holds a copy of the spent token
    assert replay.status_code == 400 and replay.json()["error"] == "invalid_grant"
    assert (await new_app.get("/me")).status_code == 401  # so the whole connection is closed
    assert (await refresh(new["refresh_token"])).status_code == 400
    await old_app.aclose()
    await new_app.aclose()


async def test_the_user_sees_and_ends_their_connections(ref, client_for, anon):
    provost = client_for(ref["users"]["provost"])
    tokens = await _connect(anon, provost, "mcp:read")
    listed = (await provost.get("/oauth/grants")).json()
    assert [(g["client_name"], g["scope"]) for g in listed] == [("Claude", "mcp:read")]
    other = client_for(ref["users"]["kok"])
    assert (await other.get("/oauth/grants")).json() == []  # not shared between users
    assert (await other.delete(f"/oauth/grants/{listed[0]['id']}")).status_code == 404
    app = _bearer(tokens, anon)
    assert (await app.get("/me")).status_code == 200
    assert (await provost.delete(f"/oauth/grants/{listed[0]['id']}")).status_code == 204
    assert (await app.get("/me")).status_code == 401
    assert (await provost.get("/oauth/grants")).json() == []
    await app.aclose()


async def test_the_app_can_revoke_itself(ref, client_for, anon):
    provost = client_for(ref["users"]["provost"])
    tokens = await _connect(anon, provost, "mcp:read")
    assert (await anon.post("/oauth/revoke", data={"token": tokens["refresh_token"]})).status_code == 200
    app = _bearer(tokens, anon)
    assert (await app.get("/me")).status_code == 401
    assert (await anon.post("/oauth/revoke", data={"token": "mcp_at_unknown"})).status_code == 200
    await app.aclose()


async def test_denying_sends_the_app_an_error_and_a_request_works_once(ref, client_for, anon):
    provost = client_for(ref["users"]["provost"])
    client_id = await _register(anon)
    request_id = await _request(anon, client_id)
    denied = await provost.post(f"/oauth/requests/{request_id}/deny")
    assert _query(denied.json()["redirect_url"]) == {"error": "access_denied", "state": "xyz"}
    assert (await provost.post(f"/oauth/requests/{request_id}/approve", json={})).status_code == 404
    assert (await provost.get("/oauth/requests/unknown")).status_code == 404


async def test_taking_a_role_away_narrows_a_connection_at_once(ref, client_for, anon):
    from app.db import pg
    from app.models import User

    provost = client_for(ref["users"]["provost"])
    app = _bearer(await _connect(anon, provost, "mcp:full", "mcp:full"), anon)
    assert (await app.get("/study-plans")).status_code == 200
    async with pg.session_scope() as session:
        user = await session.get(User, ref["users"]["provost"].id)
        user.is_active = False
    assert (await app.get("/study-plans")).status_code == 403
    await app.aclose()


def test_scope_helpers():
    assert scopes.highest(["mcp:read", "mcp:full", "other"]) == "mcp:full"
    assert scopes.highest(["other"]) is None
    assert scopes.allowed_tiers({Role.professor}) == ["read", "write"]
    assert scopes.allowed_tiers({Role.provost}) == ["read", "write", "full"]
    assert scopes.clip("mcp:full", {Role.school_admin}) == "mcp:write"
    assert scopes.permits("mcp:read", "GET", "/syllabi/{syllabus_id}")
    assert not scopes.permits("mcp:read", "POST", "/study-plans")
    assert scopes.permits("mcp:write", "POST", "/study-plans")
    assert not scopes.permits("mcp:write", "POST", "/analysis/report/send")
    assert scopes.permits("mcp:full", "POST", "/analysis/report/send")
    assert not scopes.permits("mcp:full", "GET", "/admin/users")
    assert not scopes.permits("garbage", "GET", "/me")
