from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    env: Literal["dev", "test", "prod"] = "dev"
    api_prefix: str = "/api/v1"
    frontend_url: str = "http://localhost:3000"
    cors_origins: list[str] = ["http://localhost:3000"]

    database_url: str = "postgresql+asyncpg://syllabus:syllabus@localhost:5432/syllabus"
    mongo_url: str = "mongodb://localhost:27017"
    mongo_db: str = "syllabus"
    redis_url: str = "redis://localhost:6379/0"

    # Celery is skipped and jobs are awaited in-process; meant for tests and quick local runs only.
    tasks_eager: bool = False

    files_root: Path = Path("./var/files")
    tmp_root: Path = Path("./var/tmp")
    max_upload_mb: int = 20

    # stub: no Google account yet, files go to drive_stub_root. google: see app/storage/drive.py.
    drive_provider: Literal["stub", "google"] = "stub"
    drive_stub_root: Path = Path("./var/drive")
    # Used for versions whose folder has no drive_folder_id.
    drive_default_folder_id: str = ""
    # true: the app reads Drive (the syllabus catalog) but never writes to it. New versions stay on local disk and
    # wait in the outbox, then go to Drive once this is switched off.
    drive_read_only: bool = False
    # DRIVE_PROVIDER=google: a service account key or an authorized-user file (OAuth refresh token), as JSON.
    # Setup: GOOGLE_DRIVE_SETUP.md.
    google_credentials_file: Path | None = None
    # Service account only: act as this Workspace user (domain-wide delegation, set up by the Workspace admin),
    # e.g. the shared school mailbox. Files then use that user's Drive and quota. Empty: the service account
    # acts as itself and can only upload into Shared Drives.
    google_impersonate_user: str = ""
    # Where the key uploaded on the Schools & Drive screen is kept (one file, mode 600). In Docker this is on
    # the /data volume that the API, the workers and beat share.
    secrets_root: Path = Path("./var/secrets")
    # How often celery beat re-reads the Schools' Drive folders (services/drive_catalog.py), with Google Drive.
    drive_sync_interval_s: int = 900

    # stub: dev only, the bearer token is `stub:<email>`. google: sign in with a Google Workspace account
    # (app/auth/google.py). http: an external auth service (app/auth/client.py). password: email + password
    # only (app/auth/passwords.py).
    auth_provider: Literal["stub", "http", "google", "password"] = "stub"
    # true: email + password sign-in next to Google (e.g. test users without a Workspace account).
    password_login: bool = False
    # AUTH_PROVIDER=google: an OAuth client of type "Web application" in Google Cloud.
    google_client_id: str = ""
    google_client_secret: str = ""
    # Only accounts of this Workspace domain may sign in (the id_token's `hd` claim).
    google_allowed_domain: str = "narxoz.kz"
    # Where Google sends people back; empty: derived from the request (fine behind a proxy only with
    # forwarded headers). Must be listed in the OAuth client's "Authorized redirect URIs".
    auth_redirect_url: str = ""
    # Signs the session cookie the app issues after a Google sign-in. At least 32 random characters.
    jwt_secret: str = ""
    session_ttl_hours: int = 12
    session_cookie_name: str = "syllabus_session"
    # true: `python -m app.cli bootstrap --if-enabled` (the migrate service, at every start) creates the Schools,
    # the rubric and the test users of every role. With ENV=prod only together with TEST_USERS_PASSWORD.
    seed_test_users: bool = False
    # The password of every test user (email + password sign-in); empty: they have no password.
    test_users_password: str = ""
    # false only for http://localhost; the cookie is HttpOnly and SameSite=Lax either way.
    cookie_secure: bool = True
    auth_service_url: str = ""
    auth_service_verify_path: str = "/verify"
    auth_service_timeout_s: float = 5

    # vllm, llamacpp (llama-server) and ollama all speak the OpenAI chat API; they differ only in how
    # thinking is switched off. fake: heuristics, no model.
    llm_provider: Literal["vllm", "llamacpp", "ollama", "fake"] = "fake"
    # Thinking models (Qwen3, ...) reason before the JSON answer. It made every check time out, so it is off
    # unless set to true.
    llm_thinking: bool = False
    llm_base_url: str = "http://localhost:8000/v1"
    llm_api_key: str = "EMPTY"
    llm_model: str = "Qwen/Qwen2.5-32B-Instruct"
    llm_model_version: str = "vllm-qwen2.5-32b"
    llm_max_concurrency: int = 4
    # Ollama only: context the model is loaded with (options.num_ctx), 0 = the server's default. The default
    # can be the model's maximum (262k for Qwen3), which does not fit on the GPU and slows every request.
    # One check request needs about 3k tokens plus the syllabus (roughly 1 token per 3 characters).
    llm_num_ctx: int = 16384
    # Ollama only: how long the model stays loaded after a request, so checks do not wait for a reload.
    llm_keep_alive: str = "30m"
    # First guess for the time-left estimate, seconds per criteria request, until real checks have been timed.
    eta_initial_call_s: float = 45
    llm_timeout_s: float = 180
    llm_max_retries: int = 3
    llm_output_language: str = "English"
    checker_version: str = "2026.10"
    evidence_match_threshold: int = 85

    precheck_ttl_s: int = 3600

    email_backend: Literal["smtp", "console"] = "console"
    smtp_host: str = "localhost"
    smtp_port: int = 587
    smtp_user: str = ""
    smtp_password: str = ""
    smtp_starttls: bool = True
    email_from: str = "syllabus-ai@narxoz.kz"

    @model_validator(mode="after")
    def _no_stubs_in_prod(self) -> "Settings":
        if self.env == "prod" and self.auth_provider == "stub":
            raise ValueError("AUTH_PROVIDER=stub accepts anyone, it cannot run in prod")
        if self.env == "prod" and self.drive_provider == "stub":
            raise ValueError("DRIVE_PROVIDER=stub keeps files out of Google Drive, it cannot run in prod")
        if self.drive_provider == "google" and not self.google_credentials_file:
            raise ValueError(
                "DRIVE_PROVIDER=google needs GOOGLE_CREDENTIALS_FILE (see GOOGLE_DRIVE_SETUP.md)"
            )
        if self.auth_provider == "http" and not self.auth_service_url:
            raise ValueError("AUTH_PROVIDER=http needs AUTH_SERVICE_URL")
        if self.auth_provider == "google":
            missing = [
                name
                for name, value in (
                    ("GOOGLE_CLIENT_ID", self.google_client_id),
                    ("GOOGLE_CLIENT_SECRET", self.google_client_secret),
                    ("JWT_SECRET", self.jwt_secret),
                )
                if not value
            ]
            if missing:
                raise ValueError(f"AUTH_PROVIDER=google needs {', '.join(missing)}")
        if self.password_enabled:
            if self.auth_provider not in ("google", "password"):
                raise ValueError("PASSWORD_LOGIN needs AUTH_PROVIDER=google (or use AUTH_PROVIDER=password)")
            if not self.jwt_secret:
                raise ValueError("password sign-in needs JWT_SECRET")
        if self.env == "prod" and self.jwt_secret and len(self.jwt_secret) < 32:
            raise ValueError("JWT_SECRET must be at least 32 characters in prod")
        return self

    @property
    def password_enabled(self) -> bool:
        return self.auth_provider == "password" or self.password_login


@lru_cache
def get_settings() -> Settings:
    return Settings()
