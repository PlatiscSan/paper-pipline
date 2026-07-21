from pathlib import Path

from paper_pipeline.local_importer import import_local_pdfs, metadata_from_pdf
from pypdf import PdfWriter


class FakeRepository:
    def __init__(self) -> None:
        self.updated: dict[str, object] = {}

    def upsert(self, record):
        return type(
            "Paper",
            (),
            {
                "id": 1,
                "doi": record.doi,
                "pmcid": "",
                "pmid": "",
                "arxiv_id": "",
                "source": record.source,
                "year": record.year,
                "title": record.title,
                "extraction_status": "pending",
            },
        )()

    def update(self, paper_id: int, **values) -> None:
        self.updated = values


def test_local_pdf_is_copied_and_queued(tmp_path: Path) -> None:
    source = tmp_path / "input" / "paper.pdf"
    source.parent.mkdir()
    writer = PdfWriter()
    writer.add_blank_page(100, 100)
    writer.add_metadata({"/Title": "Local catalyst paper", "/Author": "A; B"})
    with source.open("wb") as handle:
        writer.write(handle)
    repository = FakeRepository()

    result = import_local_pdfs(repository, tmp_path / "managed", source)

    assert result == {"discovered": 1, "imported": 1, "invalid": 0, "failed": 0}
    assert Path(repository.updated["pdf_path"]).exists()  # type: ignore[arg-type]
    assert repository.updated["download_method"] == "local_import"
    assert metadata_from_pdf(source).authors == ["A", "B"]
