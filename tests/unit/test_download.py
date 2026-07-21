from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from paper_pipeline.config import Settings
from paper_pipeline.db.models import Paper
from paper_pipeline.download.resolver import Resolver, landing_pdf_candidates
from paper_pipeline.download.service import DownloadService
from paper_pipeline.download.storage import is_pdf, safe_filename


def test_safe_filename() -> None:
    assert safe_filename("CON") == "_CON"
    assert safe_filename("a<b>:c/d") == "a_b__c_d"


def test_pdf_header(tmp_path: Path) -> None:
    good = tmp_path / "good.pdf"
    good.write_bytes(b"%PDF-1.7\n")
    bad = tmp_path / "bad.pdf"
    bad.write_bytes(b"<html>")
    assert is_pdf(good) and not is_pdf(bad)


def test_landing_page_discovers_public_pdf_buttons() -> None:
    html = """
      <meta content="/article/main.pdf" name="citation_pdf_url">
      <a href="/article/view-pdf">Open PDF</a>
      <a href="/login?next=paper.pdf">Log in to download PDF</a>
      <a href="javascript:void(0)">PDF</a>
    """

    candidates = landing_pdf_candidates(html, "https://journal.example/paper/1")

    assert [(item.url, item.method) for item in candidates] == [
        ("https://journal.example/article/main.pdf", "citation_pdf_url"),
        ("https://journal.example/article/view-pdf", "landing_pdf_link"),
    ]


@pytest.mark.asyncio
async def test_pmc_includes_europe_pmc_full_text_candidate() -> None:
    resolver = Resolver(None)  # type: ignore[arg-type]
    resolver._pmc = AsyncMock(return_value=[])  # type: ignore[method-assign]
    paper = Paper(pmcid="PMC123", title="Paper", canonical_key="pmcid:PMC123")

    candidates = await resolver.candidates(paper)

    assert candidates[0].method == "europe_pmc"
    assert candidates[0].url.endswith("/PMC123/fullTextPDF")


@pytest.mark.asyncio
async def test_publisher_apis_are_routed_by_doi_owner() -> None:
    resolver = Resolver(None, springer_key="springer")  # type: ignore[arg-type]
    resolver._springer = AsyncMock(return_value=[])  # type: ignore[method-assign]
    wiley = Paper(doi="10.1002/advs.1", title="Wiley", canonical_key="doi:10.1002/advs.1")
    assert not [item for item in await resolver.candidates(wiley) if "api" in item.method]
    resolver._springer.assert_not_awaited()


def test_plos_publisher_pdf_candidate() -> None:
    resolver = Resolver(None)  # type: ignore[arg-type]

    candidates = resolver._plos("10.1371/journal.pone.0170929")

    assert candidates[0].method == "plos"
    assert candidates[0].url == (
        "https://journals.plos.org/plosone/article/file"
        "?id=10.1371/journal.pone.0170929&type=printable"
    )


@pytest.mark.asyncio
async def test_empty_download_batch_logs_actionable_message(caplog) -> None:
    class EmptyRepository:
        def recover_in_progress(self, stage: str) -> int:
            return 0

        def candidates(self, stage: str, include_failed: bool = False) -> list[Paper]:
            return []

    service = DownloadService(EmptyRepository(), Settings())  # type: ignore[arg-type]

    with caplog.at_level("INFO"):
        result = await service.run()

    assert result == {"candidates": 0, "recovered": 0}
    assert "No pending downloads" in caplog.text
