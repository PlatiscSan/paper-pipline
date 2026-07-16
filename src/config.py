"""TOML configuration loading with environment expansion and path resolution."""

import os
import re
import tomllib
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, Field, SecretStr, model_validator


class CrawlerConfig(BaseModel):
    sources: list[str] = [
        "arxiv",
        "pubmed",
        "crossref",
        "europe_pmc",
        "semantic_scholar",
        "openalex",
    ]
    total: int = 100
    concurrency: int = 5
    delay_seconds: float = 0.5
    semantic_scholar_api_key: SecretStr = SecretStr("")
    openalex_api_key: SecretStr = SecretStr("")


class DownloaderConfig(BaseModel):
    concurrency: int = 10
    delay_seconds: float = 0.2
    retries: int = 3
    max_size_mb: int = 100
    semantic_scholar_api_key: SecretStr = SecretStr("")
    springer_nature_api_key: SecretStr = SecretStr("")
    elsevier_api_key: SecretStr = SecretStr("")
    use_publisher_apis: bool = True
    use_unpaywall: bool = True
    use_semantic_scholar: bool = True
    use_crossref: bool = True
    inspect_landing_pages: bool = True
    resume: bool = True


class ProviderConfig(BaseModel):
    name: str = "custom-provider"
    base_url: str = "https://example.com/v1"
    api_key: SecretStr = SecretStr("")
    model: str = "replace-with-model-id"
    api_style: Literal["chat_completions", "responses"] = "chat_completions"
    pdf_mode: Literal["text", "file"] = "text"
    structured_output: Literal["strict", "json", "prompt", "auto"] = "auto"
    output_token_param: str = "max_tokens"
    allow_empty_api_key: bool = False
    keep_remote_file: bool = False
    text_chunk_chars: int = 80000
    max_text_chunks: int = 0
    extra_headers: dict[str, str] = {}
    request_options: dict[str, Any] = {}

    @model_validator(mode="after")
    def compatible_modes(self) -> "ProviderConfig":
        if (self.api_style, self.pdf_mode) not in {
            ("chat_completions", "text"),
            ("responses", "file"),
        }:
            raise ValueError("chat_completions requires text; responses requires file")
        return self


class ExtractionConfig(BaseModel):
    profile: str = "catalysis"
    schema_path: Path = Field(default=Path("schemas/catalysis.json"), alias="schema")
    concurrency: int = 4
    retries: int = 4
    timeout_seconds: float = 600
    max_output_tokens: int = 12000
    provider: ProviderConfig = ProviderConfig()


class Settings(BaseModel):
    database_url: str = "sqlite:///data/papers.db"
    papers_dir: Path = Path("data/papers")
    academic_email: str = ""
    crawler: CrawlerConfig = CrawlerConfig()
    downloader: DownloaderConfig = DownloaderConfig()
    extraction: ExtractionConfig = ExtractionConfig()
    config_path: Path | None = None


def _expand(value: Any) -> Any:
    if isinstance(value, str):
        return re.sub(r"\$\{([^}]+)\}", lambda m: os.environ.get(m.group(1), ""), value)
    if isinstance(value, dict):
        return {key: _expand(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_expand(item) for item in value]
    return value


def load_settings(
    path: Path = Path("pipeline.toml"), overrides: dict[str, Any] | None = None
) -> Settings:
    data: dict[str, Any] = {}
    if path.exists():
        with path.open("rb") as handle:
            data = _expand(tomllib.load(handle))
    if overrides:
        data.update(overrides)
    settings = Settings.model_validate(data)
    base = path.resolve().parent
    settings.config_path = path.resolve()
    if settings.database_url.startswith("sqlite:///"):
        db_path = Path(settings.database_url.removeprefix("sqlite:///"))
        if not db_path.is_absolute():
            settings.database_url = f"sqlite:///{(base / db_path).resolve().as_posix()}"
    if not settings.papers_dir.is_absolute():
        settings.papers_dir = (base / settings.papers_dir).resolve()
    if not settings.extraction.schema_path.is_absolute():
        settings.extraction.schema_path = (base / settings.extraction.schema_path).resolve()
    return settings
