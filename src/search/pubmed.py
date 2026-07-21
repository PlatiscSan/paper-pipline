"""PubMed E-utilities provider with recursive XML text extraction."""

import asyncio
import xml.etree.ElementTree as ET
from urllib.parse import urlencode

import aiohttp
from paper_pipeline.models import PaperRecord


def recursive_text(element: ET.Element | None) -> str:
    return " ".join("".join(element.itertext()).split()) if element is not None else ""


class PubMedProvider:
    name = "pubmed"
    base = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils"

    def __init__(
        self, session: aiohttp.ClientSession, email: str = "", page_size: int = 100
    ) -> None:
        self.session, self.email, self.page_size = session, email, page_size

    async def search(
        self, keyword: str, limit: int, years: tuple[int, int] | None
    ) -> list[PaperRecord]:
        peer_reviewed = (
            " AND (journal article[pt] OR review[pt] OR systematic review[pt] "
            "OR meta-analysis[pt]) NOT preprint[pt]"
        )
        term = keyword + peer_reviewed
        term += f" AND {years[0]}:{years[1]}[pdat]" if years else ""
        params = {
            "db": "pubmed",
            "term": term,
            "retmax": limit,
            "retmode": "json",
            "email": self.email,
        }
        async with self.session.get(f"{self.base}/esearch.fcgi?{urlencode(params)}") as response:
            response.raise_for_status()
            ids = (await response.json())["esearchresult"]["idlist"]
        tasks = [
            self._fetch(ids[i : i + self.page_size], keyword)
            for i in range(0, len(ids), self.page_size)
        ]
        return [paper for page in await asyncio.gather(*tasks) for paper in page]

    async def _fetch(self, ids: list[str], keyword: str) -> list[PaperRecord]:
        if not ids:
            return []
        params = {"db": "pubmed", "id": ",".join(ids), "retmode": "xml", "email": self.email}
        async with self.session.get(f"{self.base}/efetch.fcgi?{urlencode(params)}") as response:
            response.raise_for_status()
            root = ET.fromstring(await response.read())
        return [parse_article(article, keyword) for article in root.findall(".//PubmedArticle")]


def parse_article(article: ET.Element, keyword: str = "") -> PaperRecord:
    citation = article.find("MedlineCitation")
    data = article.find("PubmedData")
    pmid = recursive_text(citation.find("PMID") if citation is not None else None)
    ids = (
        {node.get("IdType", ""): recursive_text(node) for node in data.findall(".//ArticleId")}
        if data is not None
        else {}
    )
    title = recursive_text(citation.find(".//ArticleTitle") if citation is not None else None)
    abstract = (
        " ".join(recursive_text(x) for x in citation.findall(".//AbstractText"))
        if citation is not None
        else ""
    )
    year_text = recursive_text(citation.find(".//PubDate/Year") if citation is not None else None)
    authors = (
        []
        if citation is None
        else [
            " ".join(
                filter(
                    None, [recursive_text(a.find("ForeName")), recursive_text(a.find("LastName"))]
                )
            )
            for a in citation.findall(".//Author")
        ]
    )
    return PaperRecord(
        title=title,
        authors=authors,
        year=int(year_text) if year_text.isdigit() else None,
        abstract=abstract,
        source="pubmed",
        source_record_id=pmid,
        pmid=pmid,
        pmcid=ids.get("pmc", ""),
        doi=ids.get("doi", ""),
        url=f"https://pubmed.ncbi.nlm.nih.gov/{pmid}/",
        keywords=[keyword] if keyword else [],
    )
