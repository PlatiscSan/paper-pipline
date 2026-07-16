"""Thin Typer command-line interface."""

import asyncio
import json
import os
import sys
from importlib import resources
from pathlib import Path
from typing import Annotated
from urllib.parse import urlparse

import typer
from paper_pipeline.config import Settings, load_settings
from paper_pipeline.db.base import Database
from paper_pipeline.db.migrations import upgrade
from paper_pipeline.db.repository import Repository
from paper_pipeline.download.service import DownloadService
from paper_pipeline.export.service import export as export_results
from paper_pipeline.extract.service import ExtractionService
from paper_pipeline.importer import import_csv as import_csv_file
from paper_pipeline.logging import configure
from paper_pipeline.search.base import parse_years
from paper_pipeline.search.service import SearchService
from rich.console import Console
from rich.table import Table

app = typer.Typer(
    no_args_is_help=True, help="Open-access paper search, download, and AI extraction pipeline."
)
console = Console()


@app.callback()
def main(
    verbose: bool = False,
    quiet: bool = False,
    log_file: Path | None = None,
    json_logs: bool = False,
) -> None:
    """Configure safe process logging shared by all commands."""
    configure(verbose=verbose, quiet=quiet, log_file=log_file, json_logs=json_logs)


def context(config: Path) -> tuple[Settings, Repository]:
    settings = load_settings(config)
    return settings, Repository(Database(settings.database_url))


@app.command()
def init(config: Path = Path("pipeline.toml"), force: bool = False) -> None:
    """Create configuration, data directories, and migrate SQLite."""
    if config.exists() and not force:
        raise typer.BadParameter(f"{config} exists; use --force to overwrite")
    config.parent.mkdir(parents=True, exist_ok=True)
    template = resources.files("paper_pipeline").joinpath("pipeline.example.toml")
    config.write_bytes(template.read_bytes())
    schema_dir = config.parent / "schemas"
    schema_dir.mkdir(parents=True, exist_ok=True)
    packaged_schemas = resources.files("paper_pipeline").joinpath("schemas")
    for name in ("catalysis.json", "generic.json"):
        destination = schema_dir / name
        if force or not destination.exists():
            destination.write_bytes(packaged_schemas.joinpath(name).read_bytes())
    settings = load_settings(config)
    settings.papers_dir.mkdir(parents=True, exist_ok=True)
    db_path = Path(settings.database_url.removeprefix("sqlite:///"))
    db_path.parent.mkdir(parents=True, exist_ok=True)
    upgrade(settings.database_url)
    console.print(f"Initialized [green]{config}[/green]")


@app.command()
def search(
    keywords: Annotated[list[str], typer.Option("--keywords")],
    sources: str = "arxiv,pubmed",
    year: str | None = None,
    total: int = 100,
    config: Path = Path("pipeline.toml"),
) -> None:
    """Search one or more keywords (repeat --keywords)."""
    settings, repo = context(config)
    email = os.getenv(settings.crawler.email_env, "")
    result = asyncio.run(
        SearchService(repo, email).run(keywords, sources.split(","), total, parse_years(year))
    )
    console.print(result)


@app.command("import-csv")
def import_csv(
    input: Annotated[
        Path,
        typer.Option(
            "--input",
            exists=True,
            file_okay=True,
            dir_okay=False,
            readable=True,
            resolve_path=True,
            help="Existing CSV file to import.",
        ),
    ],
    config: Path = Path("pipeline.toml"),
) -> None:
    """Import metadata and existing valid PDFs from CSV."""
    _, repo = context(config)
    console.print(import_csv_file(repo, input))


@app.command()
def download(concurrency: int | None = None, config: Path = Path("pipeline.toml")) -> None:
    """Resolve and download open-access PDFs."""
    settings, repo = context(config)
    console.print(asyncio.run(DownloadService(repo, settings).run(concurrency)))


@app.command()
def extract(
    concurrency: int | None = None, schema: Path | None = None, config: Path = Path("pipeline.toml")
) -> None:
    """Extract structured data from downloaded PDFs."""
    settings, repo = context(config)
    if schema:
        settings.extraction.schema_path = schema.resolve()
    console.print(asyncio.run(ExtractionService(repo, settings).run(concurrency)))


@app.command()
def run(
    keywords: Annotated[list[str], typer.Option("--keywords")],
    total: int = 100,
    skip_download: bool = False,
    skip_extraction: bool = False,
    search_concurrency: int | None = None,
    download_concurrency: int | None = None,
    extract_concurrency: int | None = None,
    config: Path = Path("pipeline.toml"),
) -> None:
    """Run search, download, and extraction in sequence."""
    settings, repo = context(config)
    failed = False
    try:
        console.print(
            asyncio.run(
                SearchService(repo, os.getenv(settings.crawler.email_env, "")).run(
                    keywords, settings.crawler.sources, total
                )
            )
        )
    except Exception as exc:
        console.print(f"[red]search failed: {exc}[/red]")
        failed = True
    if not skip_download:
        result = asyncio.run(DownloadService(repo, settings).run(download_concurrency))
        console.print(result)
        failed |= result.get("failed", 0) > 0
    if not skip_extraction:
        result = asyncio.run(ExtractionService(repo, settings).run(extract_concurrency))
        console.print(result)
        failed |= result.get("failed", 0) > 0
    if failed:
        raise typer.Exit(1)


@app.command()
def resume(retry_failed: bool = False, config: Path = Path("pipeline.toml")) -> None:
    """Continue work from durable database state."""
    settings, repo = context(config)
    console.print(asyncio.run(DownloadService(repo, settings).run(include_failed=retry_failed)))
    console.print(asyncio.run(ExtractionService(repo, settings).run(include_failed=retry_failed)))


@app.command()
def status(config: Path = Path("pipeline.toml")) -> None:
    """Show durable pipeline statistics."""
    _, repo = context(config)
    values = repo.status()
    table = Table("Metric", "Value")
    for key, value in values.items():
        table.add_row(key, json.dumps(value, ensure_ascii=False))
    console.print(table)


@app.command()
def retry(
    stage: Annotated[str, typer.Option("--stage")],
    include_unavailable: bool = False,
    force: bool = False,
    config: Path = Path("pipeline.toml"),
) -> None:
    """Reset failed download or extraction records to pending."""
    if stage not in {"download", "extract"}:
        raise typer.BadParameter("stage must be download or extract")
    _, repo = context(config)
    console.print({"reset": repo.retry(stage, include_unavailable, force)})


@app.command("export")
def export_command(
    format: Annotated[str, typer.Option("--format")],
    output: Annotated[Path, typer.Option("--output")],
    only_downloaded: bool = False,
    only_extracted: bool = False,
    source: str | None = None,
    year: int | None = None,
    keyword: str | None = None,
    config: Path = Path("pipeline.toml"),
) -> None:
    """Export CSV or nested JSONL."""
    if format not in {"csv", "jsonl"}:
        raise typer.BadParameter("format must be csv or jsonl")
    _, repo = context(config)
    console.print(
        {
            "exported": export_results(
                repo, output, format, only_downloaded, only_extracted, source, year, keyword
            )
        }
    )


@app.command()
def doctor(probe: bool = False, config: Path = Path("pipeline.toml")) -> None:
    """Check local runtime and provider configuration without revealing secrets."""
    checks: list[tuple[str, bool, str]] = [
        ("Python 3.11+", sys.version_info >= (3, 11), sys.version.split()[0]),
        ("Configuration", config.exists(), str(config)),
    ]
    try:
        settings, repo = context(config)
        repo.status()
        checks += [
            ("Database", True, settings.database_url.split("?")[0]),
            (
                "Data directory",
                os.access(settings.papers_dir.parent, os.W_OK),
                str(settings.papers_dir),
            ),
            (
                "AI key",
                bool(os.getenv(settings.extraction.provider.api_key_env))
                or settings.extraction.provider.allow_empty_api_key,
                f"environment {settings.extraction.provider.api_key_env}",
            ),
            (
                "base_url",
                bool(urlparse(settings.extraction.provider.base_url).scheme),
                settings.extraction.provider.base_url,
            ),
            ("model", bool(settings.extraction.provider.model), settings.extraction.provider.model),
        ]
        try:
            import fitz  # noqa: F401
        except ImportError:
            import pypdf  # noqa: F401
        checks.append(("PDF library", True, "available"))
    except Exception as exc:
        checks.append(("Configuration/database", False, str(exc)))
    table = Table("Check", "OK", "Detail")
    for name, ok, detail in checks:
        table.add_row(name, "yes" if ok else "no", detail)
    console.print(table)
    if not all(item[1] for item in checks):
        raise typer.Exit(1)
