"""LLM access. `VllmProvider` talks to any OpenAI-compatible server (vLLM, llama.cpp's llama-server, OpenRouter);
`OllamaProvider` uses Ollama's native API, which can set the context size per request; `FakeProvider`
(app.ai.fake) is for tests/dev."""

import asyncio
import json
import logging
import re
import time
import uuid
from dataclasses import dataclass
from typing import Any, Protocol

import httpx
import openai

from app.config import get_settings

log = logging.getLogger(__name__)


class LLMError(Exception):
    pass


class LLMProvider(Protocol):
    model_version: str

    async def complete_json(
        self,
        *,
        purpose: str,
        system: str,
        user: str,
        schema: dict[str, Any],
        meta: dict[str, Any] | None = None,
        run_id: uuid.UUID | None = None,
        trace: bool = True,
    ) -> dict[str, Any]: ...

    async def complete_text(
        self,
        *,
        purpose: str,
        system: str,
        user: str,
        meta: dict[str, Any] | None = None,
        trace: bool = True,
    ) -> str: ...


async def _save_trace(**fields: Any) -> None:
    from app.documents import LlmTrace

    try:
        await LlmTrace(**fields).insert()
    except Exception:  # tracing must never fail a check
        log.exception("failed to store LLM trace")


# Each OpenAI-compatible server takes a different field to switch thinking off; vLLM and llama-server pass
# chat_template_kwargs to the model's chat template. Ollama has its own API (OllamaProvider).
_NO_THINKING: dict[str, dict[str, Any]] = {
    "llamacpp": {"chat_template_kwargs": {"enable_thinking": False}},
    "vllm": {"chat_template_kwargs": {"enable_thinking": False}},
}

# llama-server started with --reasoning-format none leaves the reasoning inside the content.
_THINK_BLOCK = re.compile(r"^\s*<think>.*?</think>\s*", re.DOTALL)


def _strip_thinking(text: str) -> str:
    return _THINK_BLOCK.sub("", text, count=1)


@dataclass
class _Reply:
    text: str
    prompt_tokens: int | None = None
    completion_tokens: int | None = None


class _RetryableError(Exception):
    """A failed request worth retrying (server error, timeout, malformed reply)."""


class _ChatProvider:
    """Retries, the concurrency limit and tracing. Subclasses only send one request (`_send`)."""

    def __init__(self) -> None:
        s = get_settings()
        self.model = s.llm_model
        self.model_version = s.llm_model_version
        self.max_retries = s.llm_max_retries

    async def _send(
        self, messages: list[dict[str, str]], schema: dict[str, Any] | None, purpose: str
    ) -> _Reply:
        raise NotImplementedError

    async def _chat(
        self,
        *,
        purpose: str,
        messages: list[dict[str, str]],
        schema: dict[str, Any] | None,
        run_id: uuid.UUID | None,
        trace: bool,
        parse: bool,
    ) -> Any:
        last_error: Exception | None = None
        for attempt in range(1, self.max_retries + 1):
            queued = time.monotonic()
            reply: _Reply | None = None
            async with _semaphore():
                # Measured from here: the wait for a free slot is queued_ms, not part of duration_ms.
                started = time.monotonic()
                try:
                    reply = await self._send(messages, schema, purpose)
                    result = json.loads(reply.text) if parse else reply.text
                    error = None
                except (_RetryableError, json.JSONDecodeError) as exc:
                    result, error, last_error = None, f"{type(exc).__name__}: {exc}", exc
                duration = time.monotonic() - started
            if trace:
                await _save_trace(
                    purpose=purpose,
                    run_id=run_id,
                    model=self.model,
                    messages=messages,
                    response=reply.text if reply else None,
                    prompt_tokens=reply.prompt_tokens if reply else None,
                    completion_tokens=reply.completion_tokens if reply else None,
                    duration_ms=int(duration * 1000),
                    queued_ms=int((started - queued) * 1000),
                    error=error,
                )
            if error is None:
                log.info(
                    "LLM %s done in %.1fs (waited %.1fs), %s tokens in, %s out",
                    purpose,
                    duration,
                    started - queued,
                    reply.prompt_tokens if reply else "?",
                    reply.completion_tokens if reply else "?",
                )
                return result
            log.warning("LLM %s attempt %s/%s failed: %s", purpose, attempt, self.max_retries, error)
            if attempt < self.max_retries:
                await asyncio.sleep(min(2**attempt, 20))
        raise LLMError(f"LLM call '{purpose}' failed after {self.max_retries} attempts: {last_error}")

    async def complete_json(
        self,
        *,
        purpose: str,
        system: str,
        user: str,
        schema: dict[str, Any],
        meta: dict[str, Any] | None = None,
        run_id: uuid.UUID | None = None,
        trace: bool = True,
    ) -> dict[str, Any]:
        messages = [{"role": "system", "content": system}, {"role": "user", "content": user}]
        return await self._chat(
            purpose=purpose, messages=messages, schema=schema, run_id=run_id, trace=trace, parse=True
        )

    async def complete_text(
        self,
        *,
        purpose: str,
        system: str,
        user: str,
        meta: dict[str, Any] | None = None,
        trace: bool = True,
    ) -> str:
        messages = [{"role": "system", "content": system}, {"role": "user", "content": user}]
        return await self._chat(
            purpose=purpose, messages=messages, schema=None, run_id=None, trace=trace, parse=False
        )


class VllmProvider(_ChatProvider):
    """OpenAI chat API: vLLM, llama-server and OpenRouter."""

    def __init__(self) -> None:
        super().__init__()
        s = get_settings()
        self.extra_body: dict[str, Any] = {} if s.llm_thinking else dict(_NO_THINKING.get(s.llm_provider, {}))
        if s.llm_provider == "openrouter":
            # OpenRouter routes each request to one of several hosts of the model, and some ignore
            # response_format. require_parameters sends it only to hosts that honour the JSON schema.
            self.extra_body["provider"] = {"require_parameters": True}
            if not s.llm_thinking:
                # Reasoning models think before the JSON answer, which is slow and billed as output tokens.
                self.extra_body["reasoning"] = {"effort": "none"}
        key = s.llm_api_key
        if s.llm_provider == "openrouter" and key in ("", "EMPTY") and s.openrouter_api_key:
            key = s.openrouter_api_key
        self.client = openai.AsyncOpenAI(
            base_url=s.llm_base_url, api_key=key, timeout=s.llm_timeout_s, max_retries=0
        )

    async def _send(
        self, messages: list[dict[str, str]], schema: dict[str, Any] | None, purpose: str
    ) -> _Reply:
        kwargs: dict[str, Any] = {"model": self.model, "messages": messages, "temperature": 0}
        if schema is not None:
            kwargs["response_format"] = {
                "type": "json_schema",
                "json_schema": {"name": purpose, "schema": schema, "strict": True},
            }
        if self.extra_body:
            kwargs["extra_body"] = self.extra_body
        try:
            completion = await self.client.chat.completions.create(**kwargs)
        except openai.APIError as exc:
            raise _RetryableError(f"{type(exc).__name__}: {exc}") from exc
        usage = completion.usage
        return _Reply(
            _strip_thinking(completion.choices[0].message.content or ""),
            getattr(usage, "prompt_tokens", None),
            getattr(usage, "completion_tokens", None),
        )


def ollama_api_url(base_url: str) -> str:
    """LLM_BASE_URL may point at Ollama's OpenAI endpoint (…/v1); the native API lives at the root."""
    return base_url.rstrip("/").removesuffix("/v1") + "/api/chat"


class OllamaProvider(_ChatProvider):
    """Ollama's native /api/chat. Unlike its OpenAI endpoint it accepts `options.num_ctx`. Without it Ollama
    loads the model with the server's default context, which can be the model's maximum (262k for Qwen3):
    the KV cache then no longer fits on the GPU and every request runs several times slower."""

    def __init__(self) -> None:
        super().__init__()
        s = get_settings()
        self.url = ollama_api_url(s.llm_base_url)
        self.think: bool | None = s.llm_thinking
        self.options: dict[str, Any] = {"temperature": 0}
        if s.llm_num_ctx:
            self.options["num_ctx"] = s.llm_num_ctx
        self.keep_alive = s.llm_keep_alive
        key = s.llm_api_key
        headers = {"Authorization": f"Bearer {key}"} if key and key != "EMPTY" else {}
        self.client = httpx.AsyncClient(timeout=s.llm_timeout_s, headers=headers)

    async def _send(
        self, messages: list[dict[str, str]], schema: dict[str, Any] | None, purpose: str
    ) -> _Reply:
        body: dict[str, Any] = {
            "model": self.model,
            "messages": messages,
            "stream": False,
            "options": self.options,
        }
        if schema is not None:
            body["format"] = schema
        if self.think is not None:
            body["think"] = self.think
        if self.keep_alive:
            body["keep_alive"] = self.keep_alive
        try:
            response = await self.client.post(self.url, json=body)
            if response.status_code == 400 and "think" in response.text and "think" in body:
                # A model without a thinking mode refuses the `think` field, even when it is false.
                log.info("model %s has no thinking mode, no longer sending `think`", self.model)
                self.think = None
                body.pop("think")
                response = await self.client.post(self.url, json=body)
            response.raise_for_status()
            data = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise _RetryableError(f"{type(exc).__name__}: {exc}") from exc
        if data.get("error"):
            raise _RetryableError(str(data["error"]))
        return _Reply(
            _strip_thinking((data.get("message") or {}).get("content") or ""),
            data.get("prompt_eval_count"),
            data.get("eval_count"),
        )


# One semaphore per event loop: Celery tasks run each job in a fresh loop.
_semaphores: dict[int, asyncio.Semaphore] = {}


def _semaphore() -> asyncio.Semaphore:
    loop_id = id(asyncio.get_running_loop())
    if loop_id not in _semaphores:
        _semaphores.clear()
        _semaphores[loop_id] = asyncio.Semaphore(get_settings().llm_max_concurrency)
    return _semaphores[loop_id]


def get_llm() -> LLMProvider:
    provider = get_settings().llm_provider
    if provider == "fake":
        from app.ai.fake import FakeProvider

        return FakeProvider()
    if provider == "ollama":
        return OllamaProvider()
    return VllmProvider()
