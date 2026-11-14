"""integrations/library.yaml, validated. ${NAME} placeholders are filled from the environment."""

import os
import re
from functools import lru_cache
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, Field

from app.config import get_settings

_PLACEHOLDER = re.compile(r"\$\{([A-Z0-9_]+)\}")


class OpacConfig(BaseModel):
    databases: list[int] = Field(default_factory=lambda: [0])


class ApiConfig(BaseModel):
    db_id: str = ""
    api_key: str = ""


class LookupConfig(BaseModel):
    max_references: int = 15
    stem_length: int = 6
    title_match: float = 0.75
    record_match: float = 0.5


class LimitsConfig(BaseModel):
    requests_per_second: float = 2
    timeout_s: float = 20
    cache_ttl_hours: int = 168


class StatusConfig(BaseModel):
    compliant_at: float = 1.0
    partial_at: float = 0.5
    count_electronic: bool = True


class LibraryConfig(BaseModel):
    provider: Literal["megapro_opac", "megapro_api", "off"] = "off"
    base_url: str = "https://megapro.narxoz.kz/MegaPro"
    extra_ca_file: Path | None = None
    megapro_opac: OpacConfig = Field(default_factory=OpacConfig)
    megapro_api: ApiConfig = Field(default_factory=ApiConfig)
    lookup: LookupConfig = Field(default_factory=LookupConfig)
    limits: LimitsConfig = Field(default_factory=LimitsConfig)
    status: StatusConfig = Field(default_factory=StatusConfig)


def _fill(value: Any) -> Any:
    if isinstance(value, str):
        return _PLACEHOLDER.sub(lambda m: os.environ.get(m.group(1), ""), value)
    if isinstance(value, dict):
        return {k: _fill(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_fill(v) for v in value]
    return value


def parse(text: str) -> LibraryConfig:
    raw = yaml.safe_load(text) or {}
    return LibraryConfig.model_validate(_fill(raw.get("library") or {}))


@lru_cache
def get_library_config() -> LibraryConfig:
    """A missing file means no lookup (provider off), so the criterion stays "Cannot verify"."""
    path = get_settings().library_config
    if not path.exists():
        return LibraryConfig()
    return parse(path.read_text(encoding="utf-8"))
