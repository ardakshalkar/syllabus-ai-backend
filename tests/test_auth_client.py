"""The auth client contract, without the databases."""

import httpx
import pytest

from app.auth.client import AuthServiceUnavailable, HttpAuthClient, InvalidToken, StubAuthClient


def _client(handler) -> HttpAuthClient:
    return HttpAuthClient(transport=httpx.MockTransport(handler))


@pytest.fixture(autouse=True)
def service_url(monkeypatch):
    from app.config import get_settings

    monkeypatch.setenv("AUTH_SERVICE_URL", "http://auth.test")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


async def test_stub_reads_the_email_from_the_token():
    identity = await StubAuthClient().verify_token("stub:Provost@demo.narxoz.kz")
    assert identity.email == "provost@demo.narxoz.kz"
    assert identity.external_id == "provost@demo.narxoz.kz"
    assert await StubAuthClient().verify_account(identity.email)


@pytest.mark.parametrize("token", ["provost@demo.narxoz.kz", "stub:", "stub:nope"])
async def test_stub_rejects_other_tokens(token):
    with pytest.raises(InvalidToken):
        await StubAuthClient().verify_token(token)


async def test_http_client_forwards_the_token_and_reads_the_identity():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["authorization"] = request.headers["authorization"]
        seen["url"] = str(request.url)
        return httpx.Response(200, json={"id": 42, "email": "Lee@narxoz.kz", "name": "Lee"})

    identity = await _client(handler).verify_token("abc")
    assert seen == {"authorization": "Bearer abc", "url": "http://auth.test/verify"}
    assert (identity.external_id, identity.email, identity.name) == ("42", "lee@narxoz.kz", "Lee")


@pytest.mark.parametrize("status", [401, 403])
async def test_http_client_rejected_token(status):
    with pytest.raises(InvalidToken):
        await _client(lambda _: httpx.Response(status)).verify_token("abc")


@pytest.mark.parametrize("response", [httpx.Response(500), httpx.Response(200, json={"id": 1})])
async def test_http_client_bad_answers_are_not_a_rejected_token(response):
    with pytest.raises(AuthServiceUnavailable):
        await _client(lambda _: response).verify_token("abc")


async def test_http_client_network_failure():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused")

    with pytest.raises(AuthServiceUnavailable):
        await _client(handler).verify_token("abc")


async def test_http_client_account_check_fails_closed():
    with pytest.raises(NotImplementedError):
        await HttpAuthClient().verify_account("lee@narxoz.kz")
