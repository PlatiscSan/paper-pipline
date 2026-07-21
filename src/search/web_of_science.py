"""Web of Science Starter API provider restricted to journal articles and reviews."""

from typing import Any

import aiohttp
from paper_pipeline.models import PaperRecord

PEER_REVIEWED_TYPES = {"article", "review", "review article"}


class WebOfScienceProvider:
    name = "web_of_science"
    endpoint = "https://api.clarivate.com/apis/wos-starter/v1/documents"

    def __init__(self, session: aiohttp.ClientSession, api_key: str = "") -> None:
        self.session, self.api_key = session, api_key

    async def search(
        self, keyword: str, limit: int, years: tuple[int, int] | None
    ) -> list[PaperRecord]:
        if not self.api_key:
            return []
        escaped = keyword.replace('"', r"\"")
        query = f'TS=("{escaped}") AND DT=(Article OR Review)'
        if years:
            query += f" AND PY=({years[0]}-{years[1]})"
        headers = {"X-ApiKey": self.api_key, "Accept": "application/json"}
        results: list[PaperRecord] = []
        page = 1
        while len(results) < limit:
            size = min(50, max(10, limit - len(results)))
            async with self.session.get(
                self.endpoint,
                params={"db": "WOS", "q": query, "limit": size, "page": page},
                headers=headers,
            ) as response:
                response.raise_for_status()
                data = await response.json()
            hits = data.get("hits") or data.get("documents") or []
            for item in hits:
                record = _parse(item, keyword)
                if record is not None:
                    results.append(record)
                    if len(results) == limit:
                        break
            metadata = data.get("metadata") or {}
            if not hits or page * size >= int(metadata.get("total") or 0):
                break
            page += 1
        return results


def _parse(item: dict[str, Any], keyword: str) -> PaperRecord | None:
    types = item.get("types") or item.get("documentTypes") or item.get("documentType") or []
    if isinstance(types, str):
        types = [types]
    if not {str(value).strip().lower() for value in types} & PEER_REVIEWED_TYPES:
        return None
    names = item.get("names") or {}
    authors_raw = names.get("authors") or item.get("authors") or []
    authors = [
        str(author.get("displayName") or author.get("wosStandard") or "")
        if isinstance(author, dict)
        else str(author)
        for author in authors_raw
    ]
    identifiers = item.get("identifiers") or {}
    source = item.get("source") or {}
    links = item.get("links") or {}
    year = source.get("publishYear") or item.get("year")
    year_text = str(year or "")
    keywords = item.get("keywords") or {}
    return PaperRecord(
        title=item.get("title") or "",
        authors=[author for author in authors if author],
        year=int(year_text) if year_text.isdigit() else None,
        source="web_of_science",
        source_record_id=item.get("uid") or item.get("UID") or "",
        url=links.get("record") or links.get("wos") or "",
        doi=identifiers.get("doi") or "",
        pmid=identifiers.get("pmid") or "",
        keywords=keywords.get("authorKeywords") or [keyword],
    )
