"""Google Workspace sign-in (app/auth/google.py, app/api/auth.py). Google is never called: the token endpoint
is mocked and id_tokens are signed with a local key that stands in for Google's."""

import time
from urllib.parse import parse_qs, urlparse

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa

from app.auth import google
from app.db import pg
from app.enums import Role
from app.services import users

CLIENT_ID = "123-test.apps.googleusercontent.com"


@pytest.fixture
def google_auth(monkeypatch):
    from app.config import get_settings

    for key, value in {
        "AUTH_PROVIDER": "google",
        "GOOGLE_CLIENT_ID": CLIENT_ID,
        "GOOGLE_CLIENT_SECRET": "secret",
        "JWT_SECRET": "x" * 40,
        "GOOGLE_ALLOWED_DOMAIN": "narxoz.kz",
        "COOKIE_SECURE": "false",
        "FRONTEND_URL": "https://syllabus-agent.narxoz.kz",
    }.items():
        monkeypatch.setenv(key, value)
    get_settings.cache_clear()
    yield
    monkeypatch.setenv("AUTH_PROVIDER", "stub")
    get_settings.cache_clear()


@pytest.fixture(scope="module")
def signing_key():
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


def _id_token(key, **claims) -> str:
    now = int(time.time())
    body = {
        "iss": "https://accounts.google.com",
        "aud": CLIENT_ID,
        "sub": "1001",
        "email": "Daniar.Aitanov@narxoz.kz",
        "email_verified": True,
        "hd": "narxoz.kz",
        "name": "Daniar Aitanov",
        "iat": now,
        "exp": now + 3600,
        **claims,
    }
    return jwt.encode(body, key, algorithm="RS256", headers={"kid": "k1"})


@pytest.fixture
def google_keys(monkeypatch, signing_key):
    class Keys:
        def get_signing_key_from_jwt(self, _token):
            return type("K", (), {"key": signing_key.public_key()})()

    monkeypatch.setattr(google, "_jwks", lambda: Keys())


def _token_endpoint(id_token: str, status: int = 200) -> httpx.MockTransport:
    return httpx.MockTransport(lambda request: httpx.Response(status, json={"id_token": id_token}))


async def test_a_workspace_account_is_accepted(google_auth, google_keys, signing_key):
    account = await google.exchange_code(
        "c", "https://x/cb", transport=_token_endpoint(_id_token(signing_key))
    )
    assert account == google.GoogleAccount(
        sub="1001", email="daniar.aitanov@narxoz.kz", name="Daniar Aitanov"
    )


@pytest.mark.parametrize(
    ("claims", "code"),
    [
        ({"hd": None, "email": "someone@gmail.com"}, "domain"),  # personal account
        ({"hd": "other.kz", "email": "x@other.kz"}, "domain"),
        ({"email_verified": False}, "domain"),
        ({"aud": "another-client"}, "google"),
        ({"exp": int(time.time()) - 600}, "google"),
        ({"iss": "https://evil.example"}, "google"),
    ],
)
async def test_other_accounts_and_bad_tokens_are_refused(google_auth, google_keys, signing_key, claims, code):
    with pytest.raises(google.GoogleSignInError) as error:
        await google.exchange_code(
            "c", "https://x/cb", transport=_token_endpoint(_id_token(signing_key, **claims))
        )
    assert error.value.code == code


async def test_a_token_signed_by_someone_else_is_refused(google_auth, google_keys):
    forged = _id_token(rsa.generate_private_key(public_exponent=65537, key_size=2048))
    with pytest.raises(google.GoogleSignInError):
        await google.exchange_code("c", "https://x/cb", transport=_token_endpoint(forged))


async def _sign_in(anon, monkeypatch, email: str, next_path: str = "/") -> httpx.Response:
    login = await anon.get("/auth/google/login", params={"next": next_path})
    assert login.status_code == 307
    target = urlparse(login.headers["location"])
    params = parse_qs(target.query)
    assert (
        target.netloc == "accounts.google.com"
        and params["client_id"] == [CLIENT_ID]
        and params["hd"] == ["narxoz.kz"]
    )

    async def exchanged(code, redirect_uri, transport=None):
        assert redirect_uri.endswith("/api/v1/auth/google/callback")
        return google.GoogleAccount(sub=f"sub-{email}", email=email, name="Real Name")

    monkeypatch.setattr(google, "exchange_code", exchanged)
    return await anon.get("/auth/google/callback", params={"code": "c", "state": params["state"][0]})


async def test_sign_in_sets_a_session_the_api_accepts(ref, anon, google_auth, monkeypatch):
    async with pg.session_scope() as session:
        await users.add_user(session, "daniar.aitanov@narxoz.kz", Role.provost)
    assert (await anon.get("/auth/config")).json()["provider"] == "google"

    callback = await _sign_in(anon, monkeypatch, "daniar.aitanov@narxoz.kz", "/#syllabi")

    assert callback.status_code == 307
    assert callback.headers["location"] == "https://syllabus-agent.narxoz.kz/#syllabi"
    cookie = callback.headers["set-cookie"]
    assert "HttpOnly" in cookie and "samesite=lax" in cookie.lower()
    me = (await anon.get("/me")).json()
    assert me["email"] == "daniar.aitanov@narxoz.kz" and me["name"] == "Real Name"
    assert [r["role"] for r in me["roles"]] == ["provost"]

    assert (await anon.post("/auth/logout")).status_code == 204
    assert (await anon.get("/me")).status_code == 401


async def test_a_state_is_used_once(ref, anon, google_auth, monkeypatch):
    async with pg.session_scope() as session:
        await users.add_user(session, "kok.person@narxoz.kz", Role.kok)
    login = await anon.get("/auth/google/login")
    state = parse_qs(urlparse(login.headers["location"]).query)["state"][0]

    async def exchanged(code, redirect_uri, transport=None):
        return google.GoogleAccount(sub="s1", email="kok.person@narxoz.kz", name=None)

    monkeypatch.setattr(google, "exchange_code", exchanged)
    assert (await anon.get("/auth/google/callback", params={"code": "c", "state": state})).status_code == 307
    again = await anon.get("/auth/google/callback", params={"code": "c", "state": state})
    assert "login_error=state" in again.headers["location"]


async def test_people_without_a_role_here_cannot_sign_in(ref, anon, google_auth, monkeypatch):
    response = await _sign_in(anon, monkeypatch, "stranger@narxoz.kz")
    assert "login_error=not_provisioned" in response.headers["location"]
    assert "set-cookie" not in response.headers


async def test_a_disabled_user_loses_access(ref, anon, google_auth, monkeypatch):
    async with pg.session_scope() as session:
        await users.add_user(session, "leaver@narxoz.kz", Role.professor)
    await _sign_in(anon, monkeypatch, "leaver@narxoz.kz")
    assert (await anon.get("/me")).status_code == 200
    async with pg.session_scope() as session:
        await users.disable_user(session, "leaver@narxoz.kz")
    assert (await anon.get("/me")).status_code == 403


async def test_a_forged_or_expired_session_is_rejected(ref, anon, google_auth):
    from app.config import get_settings

    name = get_settings().session_cookie_name
    expired = jwt.encode(
        {"sub": "1", "email": "a@narxoz.kz", "exp": int(time.time()) - 10}, "x" * 40, "HS256"
    )
    forged = jwt.encode(
        {"sub": "1", "email": "a@narxoz.kz", "exp": int(time.time()) + 999}, "y" * 40, "HS256"
    )
    for token in (expired, forged):
        anon.cookies.set(name, token)
        assert (await anon.get("/me")).status_code == 401
    anon.cookies.clear()


async def test_users_cli_service(ref):
    async with pg.session_scope() as session:
        user, created = await users.add_user(
            session, "Admin.Econ@narxoz.kz", Role.school_admin, school="economics"
        )
        assert created and user.email == "admin.econ@narxoz.kz"
        with pytest.raises(users.UserError, match="--school is required"):
            await users.add_user(session, "x@narxoz.kz", Role.school_admin)
        with pytest.raises(users.UserError, match="not tied to a School"):
            await users.add_user(session, "x@narxoz.kz", Role.provost, school="law")
        listed = dict((u.email, roles) for u, roles in await users.list_users(session))
    assert listed["admin.econ@narxoz.kz"] == ["school_admin (School of Economics and Management)"]


def test_google_settings_are_required(monkeypatch):
    from app.config import Settings

    with pytest.raises(ValueError, match="GOOGLE_CLIENT_ID, GOOGLE_CLIENT_SECRET, JWT_SECRET"):
        Settings(auth_provider="google", google_client_id="", google_client_secret="", jwt_secret="")
    with pytest.raises(ValueError, match="at least 32"):
        Settings(
            env="prod",
            auth_provider="google",
            google_client_id="i",
            google_client_secret="s",
            jwt_secret="short",
            drive_provider="google",
            google_credentials_file="/x",
        )
