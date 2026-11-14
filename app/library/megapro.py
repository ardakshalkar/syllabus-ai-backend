"""MegaPro, the Narxoz Scientific Library's catalogue (megapro.narxoz.kz).

Two ways in:
- the public web catalogue (OPAC), anonymous and read-only: a search is a form POST, and the results page
  is HTML. MegaPro keeps the search and the chosen database in the ASP.NET session, so one client holds one
  session and its searches run one after another.
- the JSON API at /MegaPro/api?Method=..., which answers `GetRecords` but needs a DbId the library has to
  issue. Until then it raises LibraryUnavailable with that reason.
"""

import asyncio
import html
import re
import ssl
import time
from dataclasses import dataclass, field
from types import TracebackType
from typing import Self

import certifi
import httpx

from app.library.config import LibraryConfig


class LibraryUnavailable(Exception):
    """The catalogue cannot be searched (not configured, unreachable, or it answered with an error)."""


@dataclass
class CatalogueRecord:
    id: str
    description: str
    authors: str = ""
    copies: int | None = None
    electronic: bool = False
    database: int = 0
    disciplines: list[str] = field(default_factory=list)

    @property
    def text(self) -> str:
        return f"{self.description} {self.authors}"


_DOC = re.compile(r'<div id="(\d+)" class="rs-doc">')
_ITEM = re.compile(r'<div class="rs-item[^"]*">(.*?)</div>', re.S)
_LABEL = re.compile(r"^\s*<b>\s*([^<:]+):\s*</b>", re.S)
_TOTAL = re.compile(r"Найдено документов\s*-\s*(\d+)")
_COPIES = re.compile(r"Всего:\s*(\d+)")
_FULLTEXT_LINK = re.compile(r'href="[^"]*/DownLoad/(?!GetBookCover)[^"]*"', re.I)


# MegaPro highlights the searched stems inside words ("<strong>Принци</strong>пы"): those tags join, others split.
_INLINE = re.compile(r"</?(?:strong|em|mark)\b[^>]*>", re.I)


def _plain(fragment: str) -> str:
    text = re.sub(r"<[^>]+>", " ", _INLINE.sub("", fragment))
    return re.sub(r"\s+", " ", html.unescape(text).replace("\xa0", " ")).strip()


def parse_results(page: str, database: int = 0) -> tuple[int, list[CatalogueRecord]]:
    """The records on one OPAC results page, and how many the search found in all."""
    total = _TOTAL.search(page)
    records: list[CatalogueRecord] = []
    starts = list(_DOC.finditer(page))
    for i, start in enumerate(starts):
        block = page[start.end() : starts[i + 1].start() if i + 1 < len(starts) else len(page)]
        block = block.split('<div class="detail">', 1)[0]  # the annotation is not the description
        description: list[str] = []
        record = CatalogueRecord(id=start.group(1), description="", database=database)
        for item in _ITEM.findall(block):
            label = _LABEL.match(item)
            value = _plain(_LABEL.sub("", item, count=1))
            if not label:
                # A record may lead with its author heading ("Имашев Б.М.") in an item of its own.
                if value and not any(s in value for s in ("Формат", "Копирование", "Подробнее")):
                    description.append(value)
                continue
            name = label.group(1).strip()
            if name == "Авторы":
                record.authors = value
            elif name == "Экземпляры":
                copies = _COPIES.search(value)
                record.copies = int(copies.group(1)) if copies else None
            elif name.startswith("Дисциплина"):
                record.disciplines = [d.strip() for d in value.split(";") if d.strip()]
        record.description = " ".join(description)
        record.electronic = (
            "Электронный ресурс" in record.description
            or "Электронная версия" in block
            or bool(_FULLTEXT_LINK.search(block))
        )
        records.append(record)
    return (int(total.group(1)) if total else len(records)), records


def _ssl_context(config: LibraryConfig) -> ssl.SSLContext:
    ctx = ssl.create_default_context(cafile=certifi.where())
    if config.extra_ca_file:
        ctx.load_verify_locations(str(config.extra_ca_file))
    return ctx


class _RateLimit:
    """At most `per_second` requests to the library from this process, shared by every run."""

    def __init__(self) -> None:
        self._next = 0.0
        self._locks: dict[int, asyncio.Lock] = {}

    async def wait(self, per_second: float) -> None:
        lock = self._locks.setdefault(id(asyncio.get_running_loop()), asyncio.Lock())
        async with lock:
            now = time.monotonic()
            if self._next > now:
                await asyncio.sleep(self._next - now)
            self._next = max(now, self._next) + 1 / per_second


_limit = _RateLimit()


class OpacClient:
    """One OPAC session. Use as `async with OpacClient(config) as opac: await opac.search(...)`."""

    def __init__(self, config: LibraryConfig, transport: httpx.AsyncBaseTransport | None = None) -> None:
        self._config = config
        self._base = config.base_url.rstrip("/")
        self._database: int | None = None
        self._http = httpx.AsyncClient(
            timeout=config.limits.timeout_s,
            follow_redirects=True,
            verify=_ssl_context(config) if transport is None else True,
            transport=transport,
            headers={"User-Agent": "NarxozSyllabusChecker/1.0 (literature availability check)"},
        )

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(
        self, exc_type: type[BaseException] | None, exc: BaseException | None, tb: TracebackType | None
    ) -> None:
        await self._http.aclose()

    async def _request(self, method: str, path: str, data: dict[str, str] | None = None) -> httpx.Response:
        await _limit.wait(self._config.limits.requests_per_second)
        try:
            response = await self._http.request(method, self._base + path, data=data)
            response.raise_for_status()
        except httpx.HTTPError as exc:
            raise LibraryUnavailable(f"{type(exc).__name__}: {exc}") from exc
        return response

    async def _use(self, database: int) -> None:
        if self._database is None:
            await self._request("GET", "/Web/Search/Simple")  # opens the session
            self._database = 0
        if database != self._database:
            await self._request("POST", "/Web/Home/ChangeDb", {"DbList": str(database)})
            self._database = database

    async def search(self, terms: str, database: int = 0) -> tuple[int, list[CatalogueRecord]]:
        """Records containing every word of `terms`, each word matched as "starts with"."""
        await self._use(database)
        response = await self._request(
            "POST",
            "/Web/SearchResult/Simple",
            # cond_words=1: all words; cond_match=2: a word may continue (stems match inflected forms).
            {"simpleCond": terms, "cond_words": "1", "cond_match": "2"},
        )
        # With nothing found MegaPro stays on the search form instead of redirecting to the results page.
        if "/SearchResult/ToPage/" not in str(response.url) and not _TOTAL.search(response.text):
            return 0, []
        return parse_results(response.text, database)


class ApiClient:
    """MegaPro's JSON API. Its search method needs a DbId from the library's MegaPro administrator."""

    def __init__(self, config: LibraryConfig) -> None:
        self._config = config

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *_: object) -> None:
        return None

    async def search(self, terms: str, database: int = 0) -> tuple[int, list[CatalogueRecord]]:
        if not self._config.megapro_api.db_id:
            raise LibraryUnavailable("the MegaPro API needs MEGAPRO_DB_ID from the library")
        raise LibraryUnavailable(
            "the MegaPro API's GetRecords parameters are not known yet; use provider megapro_opac"
        )


def client(config: LibraryConfig) -> OpacClient | ApiClient:
    if config.provider == "megapro_api":
        return ApiClient(config)
    return OpacClient(config)
