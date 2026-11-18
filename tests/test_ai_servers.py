"""Where the AI runs and whether it is paid, from the configured addresses (app/services/ai_servers.py)."""

import pytest

from app.config import get_settings
from app.services import ai_servers, cost_estimate
from app.services.check_mode import SINGLE, CheckMode

FAKE_DNS = {
    "openrouter.ai": "104.18.3.115",
    "generativelanguage.googleapis.com": "142.250.74.10",
    "api.openai.com": "162.159.140.245",
    "llm.narxoz.local": "10.20.0.5",
    "ai.example.com": "93.184.216.34",
    "localhost": "127.0.0.1",
}


@pytest.fixture
def configure(monkeypatch):
    async def resolve(hostname: str) -> str | None:
        return hostname if hostname[0].isdigit() else FAKE_DNS.get(hostname)

    monkeypatch.setattr(ai_servers, "_resolve", resolve)

    def apply(**env: str) -> None:
        for key in ("DECISION_MODEL", "LLM_BILLING"):
            monkeypatch.delenv(key, raising=False)
        for key, value in env.items():
            monkeypatch.setenv(key, value)
        get_settings.cache_clear()

    yield apply
    get_settings.cache_clear()


@pytest.mark.parametrize(
    ("provider", "url", "kind", "name", "network", "billed"),
    [
        ("openrouter", "https://openrouter.ai/api/v1", "openrouter", "OpenRouter", "internet", True),
        (
            "vllm",
            "https://generativelanguage.googleapis.com/v1beta/openai/",
            "paid_api",
            "Google Gemini API",
            "internet",
            True,
        ),
        ("vllm", "https://api.openai.com/v1", "paid_api", "OpenAI (ChatGPT)", "internet", True),
        ("ollama", "http://192.168.13.79:11434", "own_server", "Ollama", "local_network", False),
        ("vllm", "http://localhost:8000/v1", "own_server", "vLLM", "this_machine", False),
        ("vllm", "http://llm.narxoz.local/v1", "own_server", "vLLM", "local_network", False),
        ("vllm", "https://ai.example.com/v1", "unknown", "vLLM", "internet", True),
    ],
)
async def test_chat_server_is_recognised_by_its_address(
    configure, provider, url, kind, name, network, billed
):
    configure(LLM_PROVIDER=provider, LLM_BASE_URL=url)
    server = await ai_servers.chat_server()
    assert (server.kind, server.name, server.network, server.billed) == (kind, name, network, billed)


async def test_host_keeps_its_port_and_address(configure):
    configure(LLM_PROVIDER="ollama", LLM_BASE_URL="http://192.168.13.79:11434")
    server = await ai_servers.chat_server()
    assert server.host == "192.168.13.79:11434" and server.address == "192.168.13.79"


async def test_billing_setting_overrides_the_guess(configure):
    configure(LLM_PROVIDER="vllm", LLM_BASE_URL="https://ai.example.com/v1", LLM_BILLING="free")
    assert (await ai_servers.chat_server()).billed is False
    configure(LLM_PROVIDER="vllm", LLM_BASE_URL="http://localhost:4000/v1", LLM_BILLING="paid")
    assert (await ai_servers.chat_server()).billed is True


async def test_own_server_with_paid_decisions_is_billed_in_standard_mode_only(configure):
    configure(
        LLM_PROVIDER="ollama",
        LLM_BASE_URL="http://192.168.13.79:11434",
        DECISION_MODEL="google/gemini-decision-1",
    )
    standard = await cost_estimate.servers(CheckMode())
    assert [s.role for s in standard] == ["chat", "decisions"]
    assert standard[1].name == "OpenRouter" and standard[1].billed
    assert await cost_estimate.billed(CheckMode()) is True
    assert await cost_estimate.billed(CheckMode(SINGLE, "qwen3-8:latest")) is False
    single = await cost_estimate.servers(CheckMode(SINGLE, "qwen3-8:latest"))
    assert [s.model for s in single] == ["qwen3-8:latest"]


async def test_test_stand_in_is_free(configure):
    configure(LLM_PROVIDER="fake", DECISION_MODEL="google/gemini-decision-1")
    assert [s.kind for s in await cost_estimate.servers(CheckMode())] == ["test"]
    assert await cost_estimate.billed() is False


def test_direct_api_models_are_priced_under_the_company_prefix(configure):
    configure(LLM_PROVIDER="vllm", LLM_BASE_URL="https://api.openai.com/v1")
    prices = {"openai/gpt-4o": (2.5e-6, 1e-5)}
    assert ai_servers.price_of("gpt-4o", prices) == (2.5e-6, 1e-5)
    configure(LLM_PROVIDER="vllm", LLM_BASE_URL="https://generativelanguage.googleapis.com/v1beta/openai/")
    assert ai_servers.price_of("models/gemini-2.5-flash", {"google/gemini-2.5-flash": (1, 2)}) == (1, 2)
    assert ai_servers.price_of("unknown-model", prices) is None
