"""Concurrent multi-provider search with deterministic quota reclamation."""

import asyncio

import aiohttp
from paper_pipeline.db.repository import Repository
from paper_pipeline.models import PaperRecord
from paper_pipeline.search.arxiv import ArxivProvider
from paper_pipeline.search.base import SearchProvider, allocate
from paper_pipeline.search.crossref import CrossrefProvider
from paper_pipeline.search.europe_pmc import EuropePMCProvider
from paper_pipeline.search.openalex import OpenAlexProvider
from paper_pipeline.search.pubmed import PubMedProvider
from paper_pipeline.search.semantic_scholar import SemanticScholarProvider
from paper_pipeline.search.web_of_science import WebOfScienceProvider

PEER_REVIEWED_SOURCES = {"web_of_science", "pubmed"}


class SearchService:
    """Search providers concurrently, then reclaim unused allocations in source order."""

    def __init__(
        self,
        repository: Repository,
        email: str = "",
        semantic_scholar_api_key: str = "",
        openalex_api_key: str = "",
        web_of_science_api_key: str = "",
        peer_reviewed_only: bool = True,
    ) -> None:
        self.repository = repository
        self.email = email
        self.semantic_scholar_api_key = semantic_scholar_api_key
        self.openalex_api_key = openalex_api_key
        self.web_of_science_api_key = web_of_science_api_key
        self.peer_reviewed_only = peer_reviewed_only

    async def run(
        self,
        keywords: list[str],
        sources: list[str],
        total: int,
        years: tuple[int, int] | None = None,
    ) -> dict[str, int]:
        if not sources:
            raise ValueError("at least one search source is required")
        if total < 1:
            raise ValueError("total must be positive")
        if "web_of_science" in sources and not self.web_of_science_api_key:
            raise ValueError(
                "web_of_science requires crawler.web_of_science_api_key in pipeline.toml"
            )
        disallowed = sorted(set(sources) - PEER_REVIEWED_SOURCES)
        if self.peer_reviewed_only and disallowed:
            raise ValueError(
                "peer_reviewed_only forbids sources without a strict journal-review filter: "
                + ", ".join(disallowed)
            )
        counts = {source: 0 for source in sources}
        initial = allocate(total, sources)
        requested = {
            (keyword, source): initial[source] for keyword in keywords for source in sources
        }
        seen_global: set[int] = set()
        seen_by_keyword: dict[str, set[int]] = {keyword: set() for keyword in keywords}
        exhausted: dict[tuple[str, str], bool] = {}

        timeout = aiohttp.ClientTimeout(total=120)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            providers: dict[str, SearchProvider] = {
                "arxiv": ArxivProvider(session),
                "pubmed": PubMedProvider(session, self.email),
                "crossref": CrossrefProvider(session, self.email),
                "europe_pmc": EuropePMCProvider(session, self.email),
                "semantic_scholar": SemanticScholarProvider(session, self.semantic_scholar_api_key),
                "openalex": OpenAlexProvider(session, self.openalex_api_key),
                "web_of_science": WebOfScienceProvider(session, self.web_of_science_api_key),
            }
            unknown = [source for source in sources if source not in providers]
            if unknown:
                raise ValueError(f"unknown search sources: {', '.join(unknown)}")

            jobs = [
                (
                    keyword,
                    source,
                    providers[source].search(keyword, initial[source], years),
                )
                for keyword in keywords
                for source in sources
            ]
            results = await asyncio.gather(*(job[2] for job in jobs), return_exceptions=True)
            for (keyword, source, _), result in zip(jobs, results, strict=True):
                key = (keyword, source)
                if isinstance(result, BaseException):
                    exhausted[key] = True
                    continue
                self._ingest(result, keyword, source, counts, seen_global, seen_by_keyword)
                exhausted[key] = len(result) < initial[source]

            for keyword in keywords:
                await self._reclaim(
                    keyword,
                    sources,
                    total,
                    years,
                    providers,
                    requested,
                    exhausted,
                    counts,
                    seen_global,
                    seen_by_keyword,
                )
        return counts

    async def _reclaim(
        self,
        keyword: str,
        sources: list[str],
        total: int,
        years: tuple[int, int] | None,
        providers: dict[str, SearchProvider],
        requested: dict[tuple[str, str], int],
        exhausted: dict[tuple[str, str], bool],
        counts: dict[str, int],
        seen_global: set[int],
        seen_by_keyword: dict[str, set[int]],
    ) -> None:
        remaining = total - len(seen_by_keyword[keyword])
        while remaining > 0:
            progress = 0
            for source in sources:
                key = (keyword, source)
                if exhausted.get(key, False):
                    continue
                new_limit = requested[key] + remaining
                try:
                    records = await providers[source].search(keyword, new_limit, years)
                except Exception:
                    exhausted[key] = True
                    continue
                requested[key] = new_limit
                exhausted[key] = len(records) < new_limit
                progress += self._ingest(
                    records, keyword, source, counts, seen_global, seen_by_keyword
                )
                remaining = total - len(seen_by_keyword[keyword])
                if remaining <= 0:
                    return
            if progress == 0:
                return

    def _ingest(
        self,
        records: list[PaperRecord],
        keyword: str,
        source: str,
        counts: dict[str, int],
        seen_global: set[int],
        seen_by_keyword: dict[str, set[int]],
    ) -> int:
        added_for_keyword = 0
        for record in records:
            paper = self.repository.upsert(record)
            if paper.id not in seen_by_keyword[keyword]:
                seen_by_keyword[keyword].add(paper.id)
                added_for_keyword += 1
            if paper.id not in seen_global:
                seen_global.add(paper.id)
                counts[source] += 1
        return added_for_keyword
