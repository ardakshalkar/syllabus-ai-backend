"""Loading Google credentials (service account, impersonation, OAuth user) and explaining Drive errors.
No network and no databases."""

import json
from unittest import mock

import httplib2
import pytest
from googleapiclient.errors import HttpError

from app.storage import drive


def _write(tmp_path, info: dict):
    path = tmp_path / "google.json"
    path.write_text(json.dumps(info))
    return path


@pytest.fixture
def service_account(monkeypatch):
    creds = mock.Mock(name="service-account-credentials")
    factory = mock.Mock(return_value=creds)
    monkeypatch.setattr("google.oauth2.service_account.Credentials.from_service_account_info", factory)
    return factory, creds


def test_service_account_key_gets_the_scope_asked_for(tmp_path, service_account):
    factory, creds = service_account
    path = _write(tmp_path, {"type": "service_account", "client_email": "bot@p.iam.gserviceaccount.com"})

    assert drive.load_credentials(path, read_only=True) is creds
    assert factory.call_args.kwargs["scopes"] == ["https://www.googleapis.com/auth/drive.readonly"]
    creds.with_subject.assert_not_called()

    drive.load_credentials(path, read_only=False)
    assert factory.call_args.kwargs["scopes"] == ["https://www.googleapis.com/auth/drive"]


def test_service_account_can_act_as_a_workspace_user(tmp_path, service_account):
    _, creds = service_account
    path = _write(tmp_path, {"type": "service_account"})

    result = drive.load_credentials(path, read_only=False, impersonate="schools@narxoz.kz")

    creds.with_subject.assert_called_once_with("schools@narxoz.kz")
    assert result is creds.with_subject.return_value


def test_impersonation_needs_a_service_account(tmp_path):
    path = _write(
        tmp_path, {"type": "authorized_user", "refresh_token": "r", "client_id": "c", "client_secret": "s"}
    )
    with pytest.raises(RuntimeError, match="only works with a service account"):
        drive.load_credentials(path, read_only=True, impersonate="schools@narxoz.kz")


@pytest.mark.parametrize(
    ("content", "message"),
    [
        (None, "does not exist"),
        ("not json", "is not JSON"),
        (json.dumps({"web": {}}), "expected a service account"),
    ],
)
def test_unusable_credentials_file_says_why(tmp_path, content, message):
    path = tmp_path / "google.json"
    if content is not None:
        path.write_text(content)
    with pytest.raises(RuntimeError, match=message):
        drive.load_credentials(path, read_only=True)


def _http_error(status: int, reason: str) -> HttpError:
    body = json.dumps({"error": {"code": status, "errors": [{"reason": reason}]}}).encode()
    return HttpError(httplib2.Response({"status": status}), body)


@pytest.mark.parametrize(
    ("error", "hint"),
    [
        (_http_error(403, "storageQuotaExceeded"), "Shared Drive"),
        (_http_error(403, "accessNotConfigured"), "API is not enabled"),
        (_http_error(404, "notFound"), "share it with the service account"),
        (_http_error(403, "insufficientFilePermissions"), "Content manager"),
    ],
)
def test_drive_errors_are_explained(error, hint):
    assert hint in drive.explain_error(error)


def test_other_errors_keep_their_text():
    assert drive.explain_error(ValueError("boom")) == "boom"
