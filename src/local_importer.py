"""Import local PDF files into pipeline-managed storage."""

import re
import shutil
from pathlib import Path
from typing import Any

from paper_pipeline.db.repository import Repository
from paper_pipeline.download.storage import destination, is_pdf
from paper_pipeline.models import PaperRecord
from pypdf import PdfReader
from pypdf.errors import PdfReadError

DOI_PATTERN = re.compile(r"10\.\d{4,9}/[-._;()/:A-Z0-9]+", re.I)
YEAR_PATTERN = re.compile(r"\b(?:19|20)\d{2}\b")


def import_local_pdfs(
    repository: Repository, papers_dir: Path, input_path: Path, recursive: bool = True
) -> dict[str, int]:
    candidates = (
        [input_path]
        if input_path.is_file()
        else sorted(input_path.rglob("*.pdf") if recursive else input_path.glob("*.pdf"))
    )
    imported = invalid = failed = 0
    for source in candidates:
        if not is_pdf(source):
            invalid += 1
            continue
        try:
            record = metadata_from_pdf(source)
            paper = repository.upsert(record)
            target = destination(papers_dir, paper)
            target.parent.mkdir(parents=True, exist_ok=True)
            if source.resolve() != target.resolve():
                shutil.copy2(source, target)
            values: dict[str, object] = {
                "pdf_path": str(target.resolve()),
                "download_status": "downloaded",
                "download_method": "local_import",
                "downloaded_bytes": target.stat().st_size,
            }
            if paper.extraction_status != "success":
                values["extraction_status"] = "pending"
            repository.update(paper.id, **values)
            imported += 1
        except (OSError, ValueError, PdfReadError):
            failed += 1
    return {
        "discovered": len(candidates),
        "imported": imported,
        "invalid": invalid,
        "failed": failed,
    }


def metadata_from_pdf(path: Path) -> PaperRecord:
    reader = PdfReader(path)
    metadata: dict[str, Any] = dict(reader.metadata or {})
    title = str(metadata.get("/Title") or "").strip() or path.stem.replace("_", " ")
    author_value = str(metadata.get("/Author") or "").strip()
    authors = [value.strip() for value in re.split(r"[;,]", author_value) if value.strip()]
    text = "\n".join((page.extract_text() or "") for page in reader.pages[:3])
    doi_match = DOI_PATTERN.search(text)
    year_match = YEAR_PATTERN.search(str(metadata.get("/CreationDate") or "") + " " + text)
    return PaperRecord(
        title=title,
        authors=authors,
        year=int(year_match.group()) if year_match else None,
        source="local",
        source_record_id=path.name,
        doi=doi_match.group().rstrip(".,;)") if doi_match else "",
    )
