"""The LLM clients (OpenAI-compatible and Ollama's native API) against a mocked server, without the databases."""

import json

import httpx
import openai
import pytest

from app.ai.checks import criteria
from app.ai.llm import OllamaProvider, VllmProvider, ollama_api_url

SCHEMA = {"type": "object", "properties": {"ok": {"type": "boolean"}}, "required": ["ok"]}


def _provider(monkeypatch, content: str, **env: str) -> tuple[VllmProvider, dict]:
    from app.config import get_settings

    monkeypatch.setenv("LLM_BASE_URL", "http://llm.test/v1")
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    get_settings.cache_clear()
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen.update(json.loads(request.content))
        message = {"role": "assistant", "content": content}
        return httpx.Response(
            200,
            json={
                "id": "x",
                "object": "chat.completion",
                "created": 0,
                "model": "m",
                "choices": [{"index": 0, "message": message, "finish_reason": "stop"}],
            },
        )

    provider = VllmProvider()
    provider.client = openai.AsyncOpenAI(
        base_url="http://llm.test/v1",
        api_key="k",
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )
    return provider, seen


@pytest.fixture(autouse=True)
def _clear_settings():
    from app.config import get_settings

    yield
    get_settings.cache_clear()


async def _ask(provider: VllmProvider) -> dict:
    return await provider.complete_json(purpose="t", system="s", user="u", schema=SCHEMA, trace=False)


@pytest.mark.parametrize(
    ("provider", "field", "value"),
    [
        ("llamacpp", "chat_template_kwargs", {"enable_thinking": False}),
        ("vllm", "chat_template_kwargs", {"enable_thinking": False}),
    ],
)
async def test_thinking_is_switched_off_the_way_each_server_expects(monkeypatch, provider, field, value):
    llm, seen = _provider(monkeypatch, '{"ok": true}', LLM_PROVIDER=provider)
    assert await _ask(llm) == {"ok": True}
    assert seen[field] == value
    assert seen["response_format"]["type"] == "json_schema"


async def test_openrouter_is_only_routed_to_hosts_that_honour_the_schema(monkeypatch):
    llm, seen = _provider(monkeypatch, '{"ok": true}', LLM_PROVIDER="openrouter")
    assert await _ask(llm) == {"ok": True}
    assert seen["provider"] == {"require_parameters": True}
    assert seen["response_format"]["type"] == "json_schema"
    assert "chat_template_kwargs" not in seen
    # OpenRouter's own switch for reasoning, so a reasoning model answers at once.
    assert seen["reasoning"] == {"effort": "none"}


async def test_thinking_can_be_left_on(monkeypatch):
    llm, seen = _provider(monkeypatch, '{"ok": true}', LLM_PROVIDER="llamacpp", LLM_THINKING="true")
    await _ask(llm)
    assert "chat_template_kwargs" not in seen and "reasoning_effort" not in seen


async def test_reasoning_left_in_the_content_is_dropped_before_parsing(monkeypatch):
    llm, _ = _provider(
        monkeypatch, '<think>\nLet me see...\n</think>\n\n{"ok": false}', LLM_PROVIDER="llamacpp"
    )
    assert await _ask(llm) == {"ok": False}


def _ollama(monkeypatch, replies: list[httpx.Response], **env: str) -> tuple[OllamaProvider, list[dict]]:
    from app.config import get_settings

    monkeypatch.setenv("LLM_PROVIDER", "ollama")
    monkeypatch.setenv("LLM_BASE_URL", "http://ollama.test:11434/v1")
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    get_settings.cache_clear()
    bodies: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/chat"
        bodies.append(json.loads(request.content))
        return replies[min(len(bodies), len(replies)) - 1]

    provider = OllamaProvider()
    provider.client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return provider, bodies


def _ollama_reply(content: str) -> httpx.Response:
    return httpx.Response(
        200,
        json={"message": {"role": "assistant", "content": content}, "prompt_eval_count": 12, "eval_count": 3},
    )


def test_ollama_native_url_is_derived_from_the_openai_one():
    assert ollama_api_url("http://h:11434/v1") == "http://h:11434/api/chat"
    assert ollama_api_url("http://h:11434/v1/") == "http://h:11434/api/chat"
    assert ollama_api_url("http://h:11434") == "http://h:11434/api/chat"


async def test_ollama_sets_the_context_size_and_switches_thinking_off(monkeypatch):
    llm, bodies = _ollama(monkeypatch, [_ollama_reply('{"ok": true}')], LLM_NUM_CTX="8192")
    assert await _ask(llm) == {"ok": True}
    body = bodies[0]
    assert body["options"] == {"temperature": 0, "num_ctx": 8192}
    assert body["think"] is False
    assert body["format"] == SCHEMA
    assert body["stream"] is False
    assert body["keep_alive"] == "30m"


async def test_ollama_model_without_thinking_mode_is_asked_again_without_the_field(monkeypatch):
    refused = httpx.Response(400, json={"error": '"m" does not support thinking'})
    llm, bodies = _ollama(monkeypatch, [refused, _ollama_reply('{"ok": true}')])
    assert await _ask(llm) == {"ok": True}
    assert "think" in bodies[0] and "think" not in bodies[1]


async def test_ollama_server_error_is_retried(monkeypatch):
    llm, bodies = _ollama(
        monkeypatch,
        [httpx.Response(500, json={"error": "busy"}), _ollama_reply('{"ok": true}')],
        LLM_MAX_RETRIES="2",
    )
    monkeypatch.setattr("app.ai.llm.asyncio.sleep", _no_sleep)
    assert await _ask(llm) == {"ok": True}
    assert len(bodies) == 2


async def _no_sleep(_: float) -> None:
    return None


def test_long_syllabus_is_cut_to_fit_the_ollama_context(monkeypatch):
    from app.config import get_settings

    monkeypatch.setenv("LLM_PROVIDER", "ollama")
    monkeypatch.setenv("LLM_NUM_CTX", "8192")
    get_settings.cache_clear()
    assert criteria.max_document_chars() == int((8192 - 4000) * 2.5)
    monkeypatch.setenv("LLM_PROVIDER", "vllm")
    get_settings.cache_clear()
    assert criteria.max_document_chars() == criteria.MAX_DOCUMENT_CHARS
