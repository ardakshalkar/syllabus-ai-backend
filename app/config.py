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

    # vllm, llamacpp (llama-server), ollama and openrouter (hosted, https://openrouter.ai/api/v1 with an API
    # key) all serve chat models; they differ only in how thinking is switched off. fake: heuristics, no model.
    llm_provider: Literal["vllm", "llamacpp", "ollama", "openrouter", "fake"] = "fake"
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
    # OpenRouter only: the hosts of LLM_MODEL to try first, in order, comma-separated (e.g. "NextBit,DeepInfra").
    # A check sends the same syllabus with every principle, and a host can serve that repeated start from its
    # prompt cache at a lower price, but only when every request of the check reaches the same host. When the
    # listed hosts are down or busy, OpenRouter falls back to the others. Empty: OpenRouter picks per request.
    llm_provider_order: str = ""
    # With a decision model: how many requests write the evidence and explanations of a syllabus. 0 sends one
    # per principle (about 9), each carrying the whole syllabus again; 1 sends every criterion in one request,
    # which reads the syllabus once; 2 or more split the principles between that many requests. On 20 syllabi
    # one request cost half as much as one per principle and lost no quotes (2026-11-17).
    explain_calls: int = 1
    # The explanation step reads the syllabus as numbered lines with a category each ([12·policy] text) and
    # cites a line by its number; the quote is then taken from that line, so it is always in the document.
    # The lines are sorted into categories once per syllabus version (app.ai.blocks) and kept.
    explain_blocks: bool = True
    # After a check, its explanations and recommendations are translated from LLM_OUTPUT_LANGUAGE into the
    # app's other languages (English, Russian, Kazakh). TRANSLATION_MODEL names a model on the same server
    # for that (e.g. a Gemini model on OpenRouter, stronger in Kazakh); empty: the chat model translates.
    translation_model: str = ""
    translate_results: bool = True
    # Longest answer for one translation request (12 findings into two languages). Answers in 712 requests up to
    # Nov 2026 were under 3,300 tokens (largest 4,355); without a cap two looped to the model's 131,072, taking
    # 9 minutes and $0.05 each. A cut-off answer is not retried: at temperature 0 it would loop again.
    translation_max_output_tokens: int = 8192
    # OpenRouter's key. LLM_PROVIDER=openrouter uses it when LLM_API_KEY is not set, and the decision model
    # always does, so one key in .env serves both.
    openrouter_api_key: str = ""
    # A decision model on OpenRouter (e.g. google/gemini-decision-1) gives every criterion its status in one
    # request; the chat model then only writes evidence, explanation and recommendation for the criteria that
    # are not Compliant. Empty: the chat model decides every status itself, one request per principle.
    decision_model: str = ""
    decision_url: str = "https://openrouter.ai/api/alpha/decisions"
    # The models the administrator can pick for the "one request" check mode (services.check_mode): one request
    # decides and explains every criterion of a syllabus. Comma-separated ids on the LLM_BASE_URL server.
    check_models: str = "google/gemma-4-31b-it,qwen/qwen4-max"
    checker_version: str = "2026.11"
    # Read a syllabus as Markdown (markitdown) and have the chat model extract its outcomes, assessment, weeks
    # and sections (app/ai/structure.py). Off, or when that fails: the line-based regex extraction.
    structure_extraction: bool = True
    evidence_match_threshold: int = 85
    # A PDF with no text layer (a phone or copier scan) is read page by page by this vision model on the same
    # server (LLM_BASE_URL), e.g. google/gemini-3.1-flash-lite on OpenRouter (app/ai/ocr.py). Empty: no OCR, and
    # such a file stays "Cannot check".
    ocr_model: str = ""
    ocr_dpi: int = 150  # pages are rendered at this resolution before they are sent
    ocr_max_pages: int = 40  # a longer scan is read up to here
    # Longest answer for one page. A normal page is under 1,300 tokens; this stops a model stuck in a loop.
    ocr_max_output_tokens: int = 4096
    # The cost estimate before a check (app/services/cost_estimate.py) measures prices from the last 30 days of
    # checks; these stand in until there is enough history. US dollars, from the Nov 2026 traces on OpenRouter.
    estimate_check_usd: float = 0.02  # checking one syllabus against the criteria
    estimate_reading_usd: float = 0.002  # reading a document's structure the first time
    estimate_ocr_page_usd: float = 0.0013  # one scanned page (gemini-3.1-flash-lite)
    estimate_decision_usd: float = (
        0.004  # one decision-model request, which OpenRouter does not list a price for
    )
    estimate_seconds_per_file: float = 30
    # A one-request check of a syllabus, until there is history for its model: tokens sent and received,
    # including the quote repair that sometimes follows (measured on 20 syllabi, Nov 2026), at the model's list price.
    estimate_single_tokens_in: int = 15_000
    estimate_single_tokens_out: int = 4_800

    precheck_ttl_s: int = 3600

    # Narxoz Scientific Library (MegaPro) lookup for the core-literature criterion. A missing file: no lookup.
    library_config: Path = Path("integrations/library.yaml")

    email_backend: Literal["smtp", "console"] = "console"
    smtp_host: str = "localhost"
    smtp_port: int = 587
    smtp_user: str = ""
    smtp_password: str = ""
    smtp_starttls: bool = True
    email_from: str = "syllabus-ai@narxoz.kz"
    # Where a generated review report (PDF) is sent: the Provost Office's shared mailbox, comma-separated for
    # several. Empty: every user holding the provost role.
    provost_office_email: str = ""
    # A folder holding the TrueType fonts the PDF report is set in (DejaVuSans.ttf and DejaVuSans-Bold.ttf, or
    # arial.ttf and arialbd.ttf). Empty: the system's DejaVu (Linux) or Arial (Windows) is used.
    report_font_dir: str = ""

    # External staff / employees API: GET /api/v1/staff/external/employees
    staff_api_base_url: str = "https://corp-portal.narxoz.kz"
    staff_api_token: str = "FTUyv78biruy32fewrh89uoefwTUVy9p8hu4t3"
    staff_api_cache_ttl_s: int = 300

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
