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


class AnswerTooLongError(LLMError):
    """The answer reached the output-token cap and was cut off. At temperature 0 the model would loop the same
    way again, so it is not retried; a check marks the request's criteria "Cannot verify" instead of failing."""


class KeyRefusedError(LLMError):
    """The server refused the API key itself (401, 402, 403: wrong key, no credit left, spending limit
    reached). Asking again cannot help, so it is never retried: every retry was another request refused
    while a check sat waiting."""


# 401 unauthorised, 402 no credit, 403 the key's spending limit ("Key limit exceeded" on OpenRouter).
KEY_REFUSED = (401, 402, 403)


def usage_cost(usage: Any) -> float | None:
    """What OpenRouter charged for a chat completion, in US dollars (its usage.cost), when it says."""
    extra = getattr(usage, "model_extra", None) or {}
    try:
        return float(extra["cost"]) if extra.get("cost") is not None else None
    except (TypeError, ValueError):
        return None


def key_refused(exc: Exception) -> KeyRefusedError | None:
    if isinstance(exc, openai.APIStatusError) and exc.status_code in KEY_REFUSED:
        return KeyRefusedError(f"the LLM server refused the API key ({exc.status_code}): {exc}")
    return None


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
    cost_usd: float | None = None
    cached_tokens: int | None = None
    provider: str | None = None


def cached_tokens(usage: Any) -> int | None:
    """Prompt tokens served from the host's prompt cache (usage.prompt_tokens_details.cached_tokens)."""
    details = getattr(usage, "prompt_tokens_details", None)
    value = getattr(details, "cached_tokens", None)
    return value if isinstance(value, int) else None


class _RetryableError(Exception):
    """A failed request worth retrying (server error, timeout, malformed reply)."""


class _TruncatedError(Exception):
    """The answer reached the output-token cap. Not retried: at temperature 0 the same request runs into the
    cap again, typically a model repeating itself."""

    def __init__(self, reply: _Reply, cap: int) -> None:
        super().__init__(f"the answer was cut off at the cap of {cap} output tokens")
        self.reply = reply


class _ChatProvider:
    """Retries, the concurrency limit and tracing. Subclasses only send one request (`_send`)."""

    def __init__(self, model: str | None = None, max_output_tokens: int | None = None) -> None:
        s = get_settings()
        self.model = model or s.llm_model
        self.model_version = model or s.llm_model_version
        self.max_retries = s.llm_max_retries
        # The longest answer a request may produce; None leaves it to the server (the model's own limit).
        self.max_output_tokens = max_output_tokens

    async def _send(
        self,
        messages: list[dict[str, str]],
        schema: dict[str, Any] | None,
        purpose: str,
        run_id: uuid.UUID | None = None,
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
                    reply = await self._send(messages, schema, purpose, run_id)
                    result = json.loads(reply.text) if parse else reply.text
                    error = None
                except (_RetryableError, json.JSONDecodeError) as exc:
                    result, error, last_error = None, f"{type(exc).__name__}: {exc}", exc
                except _TruncatedError as exc:
                    reply, result, error, last_error = exc.reply, None, f"{type(exc).__name__}: {exc}", exc
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
                    cost_usd=reply.cost_usd if reply else None,
                    cached_tokens=reply.cached_tokens if reply else None,
                    provider=reply.provider if reply else None,
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
            if isinstance(last_error, _TruncatedError):
                raise AnswerTooLongError(f"LLM call '{purpose}': {last_error}")
            if attempt < self.max_retries:
                await asyncio.sleep(min(2**attempt, 20))
        raise LLMError(f"LLM call '{purpose}' failed after {attempt} attempt(s): {last_error}")

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


def api_key() -> str:
    """The key for LLM_BASE_URL: LLM_API_KEY, or for OpenRouter OPENROUTER_API_KEY when that is not set."""
    s = get_settings()
    if s.llm_provider == "openrouter" and s.llm_api_key in ("", "EMPTY") and s.openrouter_api_key:
        return s.openrouter_api_key
    return s.llm_api_key


class VllmProvider(_ChatProvider):
    """OpenAI chat API: vLLM, llama-server and OpenRouter."""

    def __init__(self, model: str | None = None, max_output_tokens: int | None = None) -> None:
        super().__init__(model, max_output_tokens)
        s = get_settings()
        self.extra_body: dict[str, Any] = {} if s.llm_thinking else dict(_NO_THINKING.get(s.llm_provider, {}))
        if s.llm_provider == "openrouter":
            # OpenRouter routes each request to one of several hosts of the model, and some ignore
            # response_format. require_parameters sends it only to hosts that honour the JSON schema.
            routing: dict[str, Any] = {"require_parameters": True}
            order = [p.strip() for p in s.llm_provider_order.split(",") if p.strip()]
            # The hosts listed are the chat model's, not a translation model's.
            if order and self.model == s.llm_model:
                routing |= {"order": order, "allow_fallbacks": True}
            self.extra_body["provider"] = routing
            # Report the cost of each request (usage.cost), which the cost estimate prefers to list prices.
            self.extra_body["usage"] = {"include": True}
            if not s.llm_thinking:
                # Reasoning models think before the JSON answer, which is slow and billed as output tokens.
                self.extra_body["reasoning"] = {"effort": "none"}
        self.openrouter = s.llm_provider == "openrouter"
        self.client = openai.AsyncOpenAI(
            base_url=s.llm_base_url, api_key=api_key(), timeout=s.llm_timeout_s, max_retries=0
        )

    async def _send(
        self,
        messages: list[dict[str, str]],
        schema: dict[str, Any] | None,
        purpose: str,
        run_id: uuid.UUID | None = None,
    ) -> _Reply:
        kwargs: dict[str, Any] = {"model": self.model, "messages": messages, "temperature": 0}
        if self.max_output_tokens:
            kwargs["max_tokens"] = self.max_output_tokens
        if schema is not None:
            kwargs["response_format"] = {
                "type": "json_schema",
                "json_schema": {"name": purpose, "schema": schema, "strict": True},
            }
        extra_body = dict(self.extra_body)
        if self.openrouter and run_id is not None:
            # One session per check: when it falls back off the listed hosts, OpenRouter keeps the check's
            # requests on the host it fell back to, whose cache then holds the syllabus.
            extra_body["session_id"] = str(run_id)
        if extra_body:
            kwargs["extra_body"] = extra_body
        try:
            completion = await self.client.chat.completions.create(**kwargs)
        except openai.APIError as exc:
            if refused := key_refused(exc):
                raise refused from exc
            raise _RetryableError(f"{type(exc).__name__}: {exc}") from exc
        usage = completion.usage
        reply = _Reply(
            _strip_thinking(completion.choices[0].message.content or ""),
            getattr(usage, "prompt_tokens", None),
            getattr(usage, "completion_tokens", None),
            usage_cost(usage),
            cached_tokens(usage),
            (completion.model_extra or {}).get("provider"),
        )
        if self.max_output_tokens and completion.choices[0].finish_reason == "length":
            raise _TruncatedError(reply, self.max_output_tokens)
        return reply


def ollama_api_url(base_url: str) -> str:
    """LLM_BASE_URL may point at Ollama's OpenAI endpoint (…/v1); the native API lives at the root."""
    return base_url.rstrip("/").removesuffix("/v1") + "/api/chat"


class OllamaProvider(_ChatProvider):
    """Ollama's native /api/chat. Unlike its OpenAI endpoint it accepts `options.num_ctx`. Without it Ollama
    loads the model with the server's default context, which can be the model's maximum (262k for Qwen3):
    the KV cache then no longer fits on the GPU and every request runs several times slower."""

    def __init__(self, model: str | None = None, max_output_tokens: int | None = None) -> None:
        super().__init__(model, max_output_tokens)
        s = get_settings()
        self.url = ollama_api_url(s.llm_base_url)
        self.think: bool | None = s.llm_thinking
        self.options: dict[str, Any] = {"temperature": 0}
        if s.llm_num_ctx:
            self.options["num_ctx"] = s.llm_num_ctx
        if max_output_tokens:
            self.options["num_predict"] = max_output_tokens
        self.keep_alive = s.llm_keep_alive
        key = s.llm_api_key
        headers = {"Authorization": f"Bearer {key}"} if key and key != "EMPTY" else {}
        self.client = httpx.AsyncClient(timeout=s.llm_timeout_s, headers=headers)

    async def _send(
        self,
        messages: list[dict[str, str]],
        schema: dict[str, Any] | None,
        purpose: str,
        run_id: uuid.UUID | None = None,
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
        reply = _Reply(
            _strip_thinking((data.get("message") or {}).get("content") or ""),
            data.get("prompt_eval_count"),
            data.get("eval_count"),
        )
        if self.max_output_tokens and data.get("done_reason") == "length":
            raise _TruncatedError(reply, self.max_output_tokens)
        return reply


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
    cap = get_settings().llm_max_output_tokens or None
    if provider == "ollama":
        return OllamaProvider(None, cap)
    return VllmProvider(None, cap)


def get_translation_llm() -> LLMProvider:
    """The model that translates finding texts: TRANSLATION_MODEL on the same server, or the chat model."""
    s = get_settings()
    if s.llm_provider == "fake":
        from app.ai.fake import FakeProvider

        return FakeProvider()
    cap = s.translation_max_output_tokens or None
    if s.llm_provider == "ollama":
        return OllamaProvider(s.translation_model or None, cap)
    return VllmProvider(s.translation_model or None, cap)


def get_structure_llm() -> LLMProvider:
    """The model that reads a syllabus's structure (app.ai.structure): STRUCTURE_MODEL on the same server, or the
    chat model."""
    s = get_settings()
    if s.llm_provider == "fake" or not s.structure_model:
        return get_llm()
    if s.llm_provider == "ollama":
        return OllamaProvider(s.structure_model)
    return VllmProvider(s.structure_model)
