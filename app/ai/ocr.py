"""Read a scanned PDF (no text layer) with a vision model, so a syllabus photographed with CamScanner or a copier
can still be checked.

Each page is rendered to a grayscale JPEG (pypdfium2, which pdfplumber already depends on) and sent to OCR_MODEL
on LLM_BASE_URL, which transcribes it as Markdown. The pages are joined in order and the rest of the pipeline
reads that Markdown as it reads markitdown's.

Why a vision model rather than Tesseract: the scans are phone photos of tables in Kazakh, Russian and English, and
Tesseract needs a system install (with rus+kaz traineddata) on every server, struggles with the table layout and
camera skew, and gives no Markdown tables. google/gemini-3.1-flash-lite read a Kazakh page correctly (ә ғ қ ң ө ұ
ү һ і, the table as a table) for about $0.002 a page and 6 s in Nov 2026, so the Law School's ~130 scans of about
8 pages cost roughly $2. Gemma 4 31B, the chat model, invented words on the same page and is not a good OCR model.

A request that keeps failing raises LLMError (the check is "Not checked" and can be retried); a document from which
nothing readable comes back raises NoTextError (the check stays "Cannot check").
"""

import asyncio
import base64
import io
import logging
import re
import time
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

from app.ai.extraction import ExtractionError, NoTextError
from app.ai.llm import LLMError, _save_trace, _semaphore, api_key
from app.config import get_settings

log = logging.getLogger(__name__)

PROMPT = (
    "Transcribe all text on this scanned page of a university course syllabus exactly as written, as Markdown. "
    "Keep the original language (Kazakh, Russian or English) and every letter, including the Kazakh letters "
    "ә ғ қ ң ө ұ ү һ і. Render tables as Markdown tables, one row per table row. Do not translate, correct, "
    "summarise or add anything, and do not describe stamps, signatures or logos. If the page has no text, "
    "output nothing. Output only the transcription."
)

# Fewer letters than this from the whole document: nothing readable came back (blank pages, a photo, a refusal).
MIN_LETTERS = 100

_FENCE = re.compile(r"^```(?:markdown|md)?\s*\n(.*?)\n?```\s*$", re.DOTALL)

Transcribe = Callable[[bytes, int], Awaitable[str]]


def enabled() -> bool:
    s = get_settings()
    return bool(s.ocr_model) and s.llm_provider != "fake"


def render_pages(path: Path, dpi: int, max_pages: int) -> tuple[list[bytes], int]:
    """The first `max_pages` pages as grayscale JPEGs, and how many pages the document has."""
    import pypdfium2 as pdfium

    try:
        document = pdfium.PdfDocument(str(path))
    except pdfium.PdfiumError as exc:
        raise ExtractionError(f"cannot open .pdf: {exc}") from exc
    images: list[bytes] = []
    try:
        total = len(document)
        for index in range(min(total, max_pages)):
            page = document[index]
            try:
                image = page.render(scale=dpi / 72).to_pil().convert("L")
            finally:
                page.close()
            buffer = io.BytesIO()
            image.save(buffer, "JPEG", quality=80)
            images.append(buffer.getvalue())
    finally:
        document.close()
    return images, total


def clean(text: str) -> str:
    """The transcription without a ```markdown fence some models wrap it in."""
    text = text.strip()
    if m := _FENCE.match(text):
        text = m.group(1).strip()
    return text


def _letters(text: str) -> int:
    return sum(1 for ch in text if ch.isalpha())


def _model_transcriber() -> Transcribe:
    import openai

    s = get_settings()
    client = openai.AsyncOpenAI(
        base_url=s.llm_base_url, api_key=api_key(), timeout=s.llm_timeout_s, max_retries=0
    )

    async def transcribe(image: bytes, number: int) -> str:
        url = "data:image/jpeg;base64," + base64.b64encode(image).decode()
        messages: list[Any] = [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": PROMPT},
                    {"type": "image_url", "image_url": {"url": url}},
                ],
            }
        ]
        # Traced without the image (hundreds of KB a page); the prompt says which page it was.
        traced = [{"role": "user", "content": f"{PROMPT}\n[page {number}: {len(image) // 1024} KB JPEG]"}]
        last_error: Exception | None = None
        for attempt in range(1, s.llm_max_retries + 1):
            queued = time.monotonic()
            async with _semaphore():
                started = time.monotonic()
                text: str | None = None
                usage = None
                error = None
                try:
                    completion = await client.chat.completions.create(
                        model=s.ocr_model, messages=messages, temperature=0
                    )
                    text = completion.choices[0].message.content or ""
                    usage = completion.usage
                except openai.APIError as exc:
                    error, last_error = f"{type(exc).__name__}: {exc}", exc
                duration = time.monotonic() - started
            await _save_trace(
                purpose="ocr",
                model=s.ocr_model,
                messages=traced,
                response=text,
                prompt_tokens=getattr(usage, "prompt_tokens", None),
                completion_tokens=getattr(usage, "completion_tokens", None),
                duration_ms=int(duration * 1000),
                queued_ms=int((started - queued) * 1000),
                error=error,
            )
            if text is not None:
                return text
            log.warning("OCR page %s attempt %s/%s failed: %s", number, attempt, s.llm_max_retries, error)
            if attempt < s.llm_max_retries:
                await asyncio.sleep(min(2**attempt, 20))
        raise LLMError(f"OCR of page {number} failed after {s.llm_max_retries} attempts: {last_error}")

    return transcribe


async def read_pdf(path: Path, transcribe: Transcribe | None = None) -> str:
    """The scanned PDF's text as Markdown, page by page. Raises NoTextError when nothing readable comes back."""
    s = get_settings()
    started = time.monotonic()
    images, total = await asyncio.to_thread(render_pages, path, s.ocr_dpi, s.ocr_max_pages)
    if not images:
        raise NoTextError("the document contains no text")
    transcribe = transcribe or _model_transcriber()
    pages = await asyncio.gather(*(transcribe(image, n) for n, image in enumerate(images, start=1)))
    markdown = "\n\n".join(text for text in map(clean, pages) if text)
    if _letters(markdown) < MIN_LETTERS:
        raise NoTextError("the document contains no text, and none could be read from its scanned pages")
    log.info(
        "OCR read %s: %s of %s pages, %s characters in %.1fs",
        path.name,
        len(images),
        total,
        len(markdown),
        time.monotonic() - started,
    )
    return markdown
