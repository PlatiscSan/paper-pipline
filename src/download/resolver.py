"""Resolve only explicitly open-access PDF candidates."""

import logging
import re
from html.parser import HTMLParser
from urllib.parse import urljoin, urlsplit

import aiohttp
from paper_pipeline.db.models import Paper
from paper_pipeline.download.models import Candidate

logger = logging.getLogger(__name__)

PDF_LABEL = re.compile(r"\b(?:open|view|download|full\s*text)?\s*pdf\b", re.I)
BLOCKED_LABELS = ("login", "log in", "sign in", "purchase", "subscribe", "institutional")
SPRINGER_DOI_PREFIXES = ("10.1007/", "10.1038/", "10.1186/")


class _PDFLinkParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.meta_urls: list[str] = []
        self.links: list[tuple[str, str]] = []
        self._href = ""
        self._label: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = {key.lower(): value or "" for key, value in attrs}
        if (
            tag.lower() == "meta"
            and values.get("name", "").lower() == "citation_pdf_url"
            and values.get("content")
        ):
            self.meta_urls.append(values["content"])
        if tag.lower() == "a":
            self._href = values.get("href") or values.get("data-href") or values.get("data-url", "")
            self._label = [values.get("title", ""), values.get("aria-label", "")]

    def handle_data(self, data: str) -> None:
        if self._href:
            self._label.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() == "a" and self._href:
            self.links.append((self._href, " ".join(self._label)))
            self._href = ""
            self._label = []


def landing_pdf_candidates(html: str, base_url: str) -> list[Candidate]:
    """Extract public PDF metadata and ordinary links without executing JavaScript."""
    parser = _PDFLinkParser()
    parser.feed(html)
    result = [Candidate(urljoin(base_url, url), "citation_pdf_url") for url in parser.meta_urls]
    for href, label in parser.links:
        absolute = urljoin(base_url, href)
        combined = f"{absolute} {label}".lower()
        if urlsplit(absolute).scheme not in {"http", "https"}:
            continue
        if any(blocked in combined for blocked in BLOCKED_LABELS):
            continue
        if ".pdf" in urlsplit(absolute).path.lower() or PDF_LABEL.search(label):
            result.append(Candidate(absolute, "landing_pdf_link"))
    unique: dict[str, Candidate] = {}
    for candidate in result:
        unique.setdefault(candidate.url, candidate)
    return list(unique.values())[:20]


class Resolver:
    def __init__(
        self,
        session: aiohttp.ClientSession,
        email: str = "",
        semantic_key: str = "",
        springer_key: str = "",
        use_publisher_apis: bool = True,
    ) -> None:
        self.session, self.email, self.semantic_key = session, email, semantic_key
        self.springer_key = springer_key
        self.use_publisher_apis = use_publisher_apis

    async def candidates(self, paper: Paper) -> list[Candidate]:
        result: list[Candidate] = []
        if paper.pdf_url:
            result.append(Candidate(paper.pdf_url, "metadata"))
        if paper.arxiv_id:
            result.append(Candidate(f"https://arxiv.org/pdf/{paper.arxiv_id}.pdf", "arxiv"))
        if paper.pmcid:
            result.append(
                Candidate(
                    f"https://www.ebi.ac.uk/europepmc/webservices/rest/{paper.pmcid}/fullTextPDF",
                    "europe_pmc",
                )
            )
            result.extend(await self._pmc(paper.pmcid))
        if paper.doi and self.email:
            result.extend(await self._unpaywall(paper.doi))
        if paper.doi and self.semantic_key:
            result.extend(await self._semantic(paper.doi))
        if paper.doi and self.use_publisher_apis:
            normalized_doi = paper.doi.strip().lower()
            plos = self._plos(paper.doi)
            if plos:
                logger.debug(
                    "Publisher lookup: doi=%s publisher=plos candidates=%d", paper.doi, len(plos)
                )
                result.extend(plos)
            if normalized_doi.startswith(SPRINGER_DOI_PREFIXES) and self.springer_key:
                result.extend(await self._springer(normalized_doi))
            elif normalized_doi.startswith(SPRINGER_DOI_PREFIXES):
                logger.debug(
                    "Publisher lookup skipped: doi=%s publisher=springer_nature reason=no_api_key",
                    paper.doi,
                )
        if paper.url:
            result.extend(await self._landing(paper.url))
        unique: dict[str, Candidate] = {}
        for item in result:
            unique.setdefault(item.url, item)
        return list(unique.values())

    async def _pmc(self, pmcid: str) -> list[Candidate]:
        url = f"https://www.ncbi.nlm.nih.gov/pmc/utils/oa/oa.fcgi?id={pmcid}"
        try:
            async with self.session.get(url) as response:
                text = await response.text()
            hrefs = re.findall(r'href="([^"]+)"', text)
            return [Candidate(x, "pmc") for x in hrefs if x.lower().endswith(".pdf")]
        except aiohttp.ClientError:
            return []

    async def _unpaywall(self, doi: str) -> list[Candidate]:
        async with self.session.get(
            f"https://api.unpaywall.org/v2/{doi}", params={"email": self.email}
        ) as r:
            if r.status != 200:
                return []
            data = await r.json()
            location = data.get("best_oa_location") or {}
            return (
                [Candidate(location["url_for_pdf"], "unpaywall")]
                if location.get("url_for_pdf")
                else []
            )

    async def _semantic(self, doi: str) -> list[Candidate]:
        headers = {"x-api-key": self.semantic_key}
        async with self.session.get(
            f"https://api.semanticscholar.org/graph/v1/paper/DOI:{doi}",
            params={"fields": "openAccessPdf"},
            headers=headers,
        ) as r:
            if r.status != 200:
                return []
            value = (await r.json()).get("openAccessPdf") or {}
            return [Candidate(value["url"], "semantic_scholar")] if value.get("url") else []

    def _plos(self, doi: str) -> list[Candidate]:
        """Build the publisher-documented printable PDF URL for PLOS DOIs."""
        normalized = doi.strip().lower()
        match = re.fullmatch(r"10\.1371/journal\.(p[a-z]+)\.[a-z0-9.]+", normalized)
        if not match:
            return []
        journal = {
            "pbio": "plosbiology",
            "pcbi": "ploscompbiol",
            "pgen": "plosgenetics",
            "pmed": "plosmedicine",
            "pntd": "plosntds",
            "ppat": "plospathogens",
            "pone": "plosone",
        }.get(match.group(1))
        if not journal:
            return []
        return [
            Candidate(
                f"https://journals.plos.org/{journal}/article/file?id={normalized}&type=printable",
                "plos",
            )
        ]

    async def _springer(self, doi: str) -> list[Candidate]:
        """Ask Springer Nature's OA API for explicitly advertised PDF URLs."""
        try:
            async with self.session.get(
                "https://api.springernature.com/openaccess/json",
                params={"api_key": self.springer_key, "q": f"doi:{doi}"},
            ) as response:
                if response.status != 200:
                    logger.debug(
                        "Publisher API response: doi=%s publisher=springer_nature "
                        "http_status=%d candidates=0",
                        doi,
                        response.status,
                    )
                    return []
                records = (await response.json()).get("records") or []
        except (aiohttp.ClientError, ValueError) as exc:
            logger.debug(
                "Publisher API error: doi=%s publisher=springer_nature error=%s",
                doi,
                type(exc).__name__,
            )
            return []
        urls: list[str] = []
        for record in records:
            for value in record.get("url") or []:
                if isinstance(value, dict):
                    candidate_url = str(value.get("value") or value.get("url") or "")
                    kind = str(value.get("format") or "").lower()
                else:
                    candidate_url, kind = str(value), ""
                if candidate_url and ("pdf" in kind or ".pdf" in candidate_url.lower()):
                    urls.append(candidate_url)
        candidates = [Candidate(url, "springer_nature_oa") for url in dict.fromkeys(urls)]
        logger.debug(
            "Publisher API response: doi=%s publisher=springer_nature "
            "http_status=200 candidates=%d",
            doi,
            len(candidates),
        )
        return candidates

    async def _landing(self, url: str) -> list[Candidate]:
        try:
            async with self.session.get(url) as response:
                if response.status != 200:
                    return []
                body = await response.text(errors="ignore")
                final_url = str(response.url)
            return landing_pdf_candidates(body, final_url)
        except aiohttp.ClientError:
            return []
