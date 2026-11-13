"""Email + password sign-in (app/auth/passwords.py, POST /auth/login)."""

import time

import jwt
import pytest
import sqlalchemy as sa

from app.auth import passwords
from app.db import pg
from app.enums import Role
from app.models import User
from app.services import users
from app.services.demo_users import add_demo_users, setup_schools

PASSWORD = "correct horse battery"


@pytest.fixture(params=["password", "google"])
def password_auth(request, monkeypatch):
    """AUTH_PROVIDER=password, and PASSWORD_LOGIN=true next to Google."""
    from app.config import get_settings

    env = {"AUTH_PROVIDER": request.param, "JWT_SECRET": "x" * 40, "COOKIE_SECURE": "false"}
    if request.param == "google":
        env |= {"PASSWORD_LOGIN": "true", "GOOGLE_CLIENT_ID": "id", "GOOGLE_CLIENT_SECRET": "secret"}
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    get_settings.cache_clear()
    yield request.param
    monkeypatch.setenv("AUTH_PROVIDER", "stub")
    monkeypatch.delenv("PASSWORD_LOGIN", raising=False)
    get_settings.cache_clear()


async def _add(email: str, role: Role = Role.provost, password: str | None = PASSWORD) -> None:
    async with pg.session_scope() as session:
        await users.add_user(session, email, role, password=password)


def test_hashes_are_salted_and_checked():
    first, second = passwords.hash_password(PASSWORD), passwords.hash_password(PASSWORD)
    assert first != second and first.startswith("scrypt$")
    assert passwords.check_password(PASSWORD, first)
    assert not passwords.check_password("wrong password", first)
    assert not passwords.check_password(PASSWORD, None)
    assert not passwords.check_password(PASSWORD, "garbage")
    with pytest.raises(ValueError):
        passwords.hash_password("short")


async def test_sign_in_sets_a_session_the_api_accepts(ref, anon, password_auth):
    await _add("Daniar.Aitanov@narxoz.kz")
    config = (await anon.get("/auth/config")).json()
    assert config["provider"] == password_auth and config["password"] is True

    login = await anon.post("/auth/login", json={"email": " DANIAR.aitanov@narxoz.kz", "password": PASSWORD})

    assert login.status_code == 204, login.text
    cookie = login.headers["set-cookie"]
    assert "HttpOnly" in cookie and "samesite=lax" in cookie.lower()
    me = (await anon.get("/me")).json()
    assert me["email"] == "daniar.aitanov@narxoz.kz" and [r["role"] for r in me["roles"]] == ["provost"]

    assert (await anon.post("/auth/logout")).status_code == 204
    assert (await anon.get("/me")).status_code == 401


async def test_wrong_password_unknown_email_and_no_password_look_the_same(ref, anon, password_auth):
    await _add("kok.person@narxoz.kz", Role.kok)
    await _add("google.only@narxoz.kz", Role.kok, password=None)
    for email, password in (
        ("kok.person@narxoz.kz", "wrong password"),
        ("nobody@narxoz.kz", PASSWORD),
        ("google.only@narxoz.kz", PASSWORD),
    ):
        response = await anon.post("/auth/login", json={"email": email, "password": password})
        assert response.status_code == 401 and response.json()["detail"] == "Wrong email or password"
        assert "set-cookie" not in response.headers


async def test_too_many_failures_lock_the_email(ref, anon, password_auth):
    await _add("target@narxoz.kz")
    for _ in range(passwords.MAX_FAILURES):
        await anon.post("/auth/login", json={"email": "target@narxoz.kz", "password": "wrong password"})
    locked = await anon.post("/auth/login", json={"email": "target@narxoz.kz", "password": PASSWORD})
    assert locked.status_code == 429


async def test_changing_the_password_or_disabling_ends_sessions(ref, anon, password_auth):
    await _add("leaver@narxoz.kz", Role.professor)
    await anon.post("/auth/login", json={"email": "leaver@narxoz.kz", "password": PASSWORD})
    assert (await anon.get("/me")).status_code == 200

    async with pg.session_scope() as session:
        await users.set_password(session, "leaver@narxoz.kz", "a brand new password")
    assert (await anon.get("/me")).status_code == 401

    await anon.post("/auth/login", json={"email": "leaver@narxoz.kz", "password": "a brand new password"})
    assert (await anon.get("/me")).status_code == 200
    async with pg.session_scope() as session:
        await users.disable_user(session, "leaver@narxoz.kz")
    assert (await anon.get("/me")).status_code == 403
    login = await anon.post(
        "/auth/login", json={"email": "leaver@narxoz.kz", "password": "a brand new password"}
    )
    assert login.status_code == 401


async def test_forged_and_expired_sessions_are_rejected(ref, anon, password_auth):
    await _add("someone@narxoz.kz")
    async with pg.session_scope() as session:
        user = await session.scalar(sa.select(User).where(User.email == "someone@narxoz.kz"))
        assert user is not None and user.password_hash
        uid, pwv = str(user.id), passwords._fingerprint(user.password_hash)
    name = "syllabus_session"
    now = int(time.time())
    for secret, exp in (("y" * 40, now + 999), ("x" * 40, now - 10)):
        anon.cookies.set(name, jwt.encode({"kind": "password", "uid": uid, "pwv": pwv, "exp": exp}, secret))
        assert (await anon.get("/me")).status_code == 401
    anon.cookies.clear()


async def test_disabled_when_not_configured(ref, anon):
    # The test stack runs AUTH_PROVIDER=stub.
    assert (await anon.get("/auth/config")).json()["password"] is False
    response = await anon.post("/auth/login", json={"email": "a@narxoz.kz", "password": PASSWORD})
    assert response.status_code == 404


async def test_test_users_get_the_password(ref, anon, password_auth):
    async with pg.session_scope() as session:
        await setup_schools(session)
        report = await add_demo_users(session, domain="t.test", password=PASSWORD)
    assert len(report.roles) == 12
    for email in ("superadmin@t.test", "provost@t.test", "admin.law@t.test", "karimova@t.test"):
        login = await anon.post("/auth/login", json={"email": email, "password": PASSWORD})
        assert login.status_code == 204, email
    anon.cookies.clear()


async def test_test_users_in_prod_need_allow_prod_and_a_password(ref, monkeypatch):
    from app.config import get_settings

    for key, value in {
        "ENV": "prod",
        "AUTH_PROVIDER": "password",
        "JWT_SECRET": "x" * 40,
        "DRIVE_PROVIDER": "google",
        "GOOGLE_CREDENTIALS_FILE": "/dev/null",
    }.items():
        monkeypatch.setenv(key, value)
    get_settings.cache_clear()
    try:
        async with pg.session_scope() as session:
            with pytest.raises(RuntimeError):
                await add_demo_users(session, domain="t.test", password=PASSWORD)
            with pytest.raises(RuntimeError):
                await add_demo_users(session, domain="t.test", allow_prod=True)
            report = await add_demo_users(session, domain="t.test", password=PASSWORD, allow_prod=True)
            assert "provost@t.test" in report.roles
    finally:
        for key in ("ENV", "DRIVE_PROVIDER", "GOOGLE_CREDENTIALS_FILE"):
            monkeypatch.delenv(key, raising=False)
        monkeypatch.setenv("ENV", "test")
        monkeypatch.setenv("AUTH_PROVIDER", "stub")
        get_settings.cache_clear()
