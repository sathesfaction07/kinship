"""Configuration from `CS_`-prefixed environment variables or a `.env` file.

No list-typed fields. Pydantic Settings parses those as JSON, so `CS_REGIONS=na,eu` fails with
a decode error that reads like a bug in the tool rather than a malformed variable.
"""

from __future__ import annotations

from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="CS_", env_file=".env", extra="ignore")

    cases: Path = Path("data/cases.jsonl")
    truth: Path = Path("data/truth.jsonl")
    output_dir: Path = Path("output")

    threshold: float = Field(default=0.85, ge=0.0, le=1.0)
    """Similarity at or above which two names are considered a match.

    Bounded by the model rather than checked at the call site, so an out-of-range value fails
    at load with a message naming the field instead of silently matching everything or nothing.
    """

    linkage: str = "single"
    max_block: int = Field(default=60, ge=2)
