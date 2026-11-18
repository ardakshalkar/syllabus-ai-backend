"""Where the AI of a check runs, and whether each use of it is paid, for the cost estimate and the settings.

A check talks to up to two servers:
- the chat server (LLM_BASE_URL): checking, reading documents, reading scans (OCR_MODEL) and translating;
- the decision service (DECISION_URL), when DECISION_MODEL is set and the check runs in the standard mode.

Each is recognised by its address:
- a known paid service (OpenRouter, Google Gemini, OpenAI/ChatGPT, Anthropic/Claude, DeepSeek, ...): paid per use;
- an address on this machine or the local network (127.0.0.1, 10.x, 192.168.x, a name resolving to one):
  the university's own server, free per use;
- any other address on the internet: not recognised; counted as paid, since most hosted models are.
LLM_BILLING=free|paid overrides the guess for the chat server (e.g. the university's own server on a public
address, or a paid service behind a local proxy).

Prices of a paid service other than OpenRouter come from OpenRouter's list under the same company's prefix
("gpt-4o" on OpenAI is priced as "openai/gpt-4o"): OpenRouter charges what the company charges.
"""

import asyncio
import ipaddress
import logging
import socket
import time
from dataclasses import asdict, dataclass
from typing import Any, Literal
from urllib.parse import urlparse

from app.config import get_settings

log = logging.getLogger(__name__)

Kind = Literal["openrouter", "paid_api", "own_server", "unknown", "test"]
Network = Literal["this_machine", "local_network", "internet", "unknown"]


@dataclass(frozen=True)
class Service:
    name: str
    price_prefix: str | None  # the company's prefix in OpenRouter's model list


# Host name suffix → the service. Checked longest first, so "aiplatform.googleapis.com" wins over a shorter one.
KNOWN: dict[str, Service] = {
    "openrouter.ai": Service("OpenRouter", None),
    "generativelanguage.googleapis.com": Service("Google Gemini API", "google"),
    "aiplatform.googleapis.com": Service("Google Vertex AI", "google"),
    "api.openai.com": Service("OpenAI (ChatGPT)", "openai"),
    "openai.azure.com": Service("Azure OpenAI", "openai"),
    "api.anthropic.com": Service("Anthropic (Claude)", "anthropic"),
    "api.deepseek.com": Service("DeepSeek", "deepseek"),
    "api.mistral.ai": Service("Mistral AI", "mistralai"),
    "api.x.ai": Service("xAI (Grok)", "x-ai"),
    "api.groq.com": Service("Groq", None),
    "api.together.xyz": Service("Together AI", None),
    "api.deepinfra.com": Service("DeepInfra", None),
    "api.fireworks.ai": Service("Fireworks AI", None),
    "dashscope.aliyuncs.com": Service("Alibaba Cloud (Qwen)", "qwen"),
    "dashscope-intl.aliyuncs.com": Service("Alibaba Cloud (Qwen)", "qwen"),
    "api.moonshot.ai": Service("Moonshot AI (Kimi)", "moonshotai"),
    "api.cohere.ai": Service("Cohere", "cohere"),
    "api.cohere.com": Service("Cohere", "cohere"),
    "api.perplexity.ai": Service("Perplexity", "perplexity"),
}

# The software of a server of our own, by LLM_PROVIDER.
SOFTWARE = {
    "ollama": "Ollama",
    "vllm": "vLLM",
    "llamacpp": "llama.cpp",
    "openrouter": "OpenAI-compatible server",
}


@dataclass(frozen=True)
class AiServer:
    role: Literal["chat", "decisions"]
    kind: Kind
    name: str  # "OpenRouter", "Google Gemini API", "Ollama", ...
    host: str  # "openrouter.ai", "192.168.13.79:11434"
    address: str | None  # the IP address it resolves to, when it could be found
    network: Network
    billed: bool
    model: str | None

    def out(self) -> dict[str, Any]:
        return asdict(self)


def service_of(host: str) -> Service | None:
    host = host.lower().rstrip(".")
    for suffix in sorted(KNOWN, key=len, reverse=True):
        if host == suffix or host.endswith("." + suffix):
            return KNOWN[suffix]
    return None


def _network(ip: str | None) -> Network:
    if not ip:
        return "unknown"
    try:
        address = ipaddress.ip_address(ip)
    except ValueError:
        return "unknown"
    if address.is_loopback:
        return "this_machine"
    # 100.64/10 (carrier-grade NAT, Tailscale) is not "private" to ipaddress but is not the public internet.
    if address.is_private or address.is_link_local or address in ipaddress.ip_network("100.64.0.0/10"):
        return "local_network"
    return "internet"


# Host → (when, IP); a name is looked up at most every 10 minutes and never holds an estimate up for long.
_resolved: dict[str, tuple[float, str | None]] = {}
RESOLVE_TIMEOUT_S = 2.0


async def _resolve(hostname: str) -> str | None:
    try:
        ipaddress.ip_address(hostname)
        return hostname
    except ValueError:
        pass
    cached = _resolved.get(hostname)
    if cached and time.monotonic() - cached[0] < 600:
        return cached[1]
    ip: str | None = None
    try:
        infos = await asyncio.wait_for(
            asyncio.get_running_loop().getaddrinfo(hostname, None, type=socket.SOCK_STREAM), RESOLVE_TIMEOUT_S
        )
        ip = str(infos[0][4][0]) if infos else None
    except (TimeoutError, OSError) as exc:
        log.info("could not resolve %s: %s", hostname, exc)
    _resolved[hostname] = (time.monotonic(), ip)
    return ip


def _host_of(url: str) -> tuple[str, str]:
    """(hostname, host with its port when one is given) of a URL."""
    parsed = urlparse(url if "://" in url else f"http://{url}")
    hostname = parsed.hostname or ""
    shown = f"[{hostname}]" if ":" in hostname else hostname
    return hostname, f"{shown}:{parsed.port}" if parsed.port else shown


def _classify(hostname: str, network: Network, provider: str) -> tuple[Kind, str]:
    service = service_of(hostname)
    if service:
        return ("openrouter" if service.name == "OpenRouter" else "paid_api"), service.name
    if network in ("this_machine", "local_network"):
        return "own_server", SOFTWARE.get(provider, "AI server")
    return "unknown", SOFTWARE.get(provider, "AI server")


async def chat_server() -> AiServer:
    s = get_settings()
    if s.llm_provider == "fake":
        return AiServer("chat", "test", "Test stand-in (no AI model)", "", None, "this_machine", False, None)
    hostname, host = _host_of(s.llm_base_url)
    ip = await _resolve(hostname)
    network = _network(ip)
    kind, name = _classify(hostname, network, s.llm_provider)
    billed = kind in ("openrouter", "paid_api", "unknown")
    if s.llm_billing != "auto":
        billed = s.llm_billing == "paid"
    return AiServer("chat", kind, name, host, ip, network, billed, s.llm_model)


def decisions_used() -> bool:
    s = get_settings()
    return bool(s.decision_model) and s.llm_provider != "fake"


async def decision_server() -> AiServer | None:
    """The decision service of the standard check mode, or None when no decision model is set."""
    s = get_settings()
    if not decisions_used():
        return None
    hostname, host = _host_of(s.decision_url)
    ip = await _resolve(hostname)
    network = _network(ip)
    kind, name = _classify(hostname, network, "")
    billed = kind in ("openrouter", "paid_api", "unknown")
    return AiServer("decisions", kind, name, host, ip, network, billed, s.decision_model)


def chat_price_prefix() -> str | None:
    """The OpenRouter prefix under which the chat server's models are priced, for a paid service other than
    OpenRouter ("openai" for api.openai.com)."""
    service = service_of(_host_of(get_settings().llm_base_url)[0])
    return service.price_prefix if service else None


def price_of(model: str, prices: dict[str, tuple[float, float]]) -> tuple[float, float] | None:
    """A chat-server model's list price: under its own id, or under its company's prefix on OpenRouter."""
    if model in prices:
        return prices[model]
    prefix = chat_price_prefix()
    # Direct APIs name a model without its company ("gemini-2.5-flash", "models/gemini-2.5-flash").
    bare = model.removeprefix("models/")
    if prefix and "/" not in bare:
        return prices.get(f"{prefix}/{bare}")
    return None
