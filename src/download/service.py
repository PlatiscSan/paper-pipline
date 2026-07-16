"""Paper-level concurrent resolution and download orchestration."""

import asyncio
import logging

import aiohttp
from paper_pipeline.config import Settings
from paper_pipeline.db.models import Paper
from paper_pipeline.db.repository import Repository
from paper_pipeline.download.client import DownloadClient
from paper_pipeline.download.models import DownloadResult
from paper_pipeline.download.resolver import Resolver
from paper_pipeline.download.storage import destination, is_pdf

logger = logging.getLogger(__name__)


class DownloadService:
    def __init__(self, repository: Repository, settings: Settings) -> None:
        self.repository, self.settings = repository, settings

    async def run(
        self, concurrency: int | None = None, include_failed: bool = False
    ) -> dict[str, int]:
        recovered = self.repository.recover_in_progress("download")
        papers = self.repository.candidates("download", include_failed)
        if not papers:
            logger.info("No pending downloads; use retry to requeue previous failures")
            return {"candidates": 0, "recovered": recovered}
        effective_concurrency = concurrency or self.settings.downloader.concurrency
        sem = asyncio.Semaphore(effective_concurrency)
        logger.info(
            "Download batch started: candidates=%d concurrency=%d recovered=%d",
            len(papers),
            effective_concurrency,
            recovered,
        )
        timeout = aiohttp.ClientTimeout(total=300, connect=30)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            resolver = Resolver(
                session,
                self.settings.academic_email,
                self.settings.downloader.semantic_scholar_api_key.get_secret_value(),
                self.settings.downloader.springer_nature_api_key.get_secret_value(),
                self.settings.downloader.elsevier_api_key.get_secret_value(),
                self.settings.downloader.use_publisher_apis,
            )
            client = DownloadClient(
                session,
                self.settings.downloader.retries,
                self.settings.downloader.delay_seconds,
                self.settings.downloader.max_size_mb,
            )

            async def one(paper: Paper) -> str:
                async with sem:
                    return await self._one(paper, resolver, client)

            pending = {asyncio.create_task(one(p)) for p in papers}
            statuses: list[str] = []
            while pending:
                done, pending = await asyncio.wait(
                    pending, timeout=15, return_when=asyncio.FIRST_COMPLETED
                )
                if not done:
                    logger.info(
                        "Download heartbeat: completed=%d/%d active=%d",
                        len(statuses),
                        len(papers),
                        len(pending),
                    )
                    continue
                for task in done:
                    status = await task
                    statuses.append(status)
                    logger.debug(
                        "Download item completed: progress=%d/%d status=%s",
                        len(statuses),
                        len(papers),
                        status,
                    )
                if len(statuses) == len(papers) or len(statuses) % 10 == 0:
                    logger.info(
                        "Download progress: completed=%d/%d active=%d",
                        len(statuses),
                        len(papers),
                        len(pending),
                    )
        summary = {name: statuses.count(name) for name in set(statuses)}
        summary["candidates"] = len(papers)
        summary["recovered"] = recovered
        logger.info("Download batch completed: %s", summary)
        return summary

    async def _one(self, paper: Paper, resolver: Resolver, client: DownloadClient) -> str:
        if paper.pdf_path and is_pdf(__import__("pathlib").Path(paper.pdf_path)):
            return "skipped"
        self.repository.update(
            paper.id, download_status="downloading", download_attempts=paper.download_attempts + 1
        )
        candidates = await resolver.candidates(paper)
        if not candidates:
            self.repository.update(
                paper.id,
                download_status="unavailable",
                download_error_code="NO_OPEN_ACCESS_PDF",
                download_error_message="no open candidate",
            )
            return "unavailable"
        last = DownloadResult()
        for candidate in candidates:
            last = await client.fetch(candidate, destination(self.settings.papers_dir, paper))
            if last.status == "downloaded":
                self.repository.update(
                    paper.id,
                    pdf_path=last.path,
                    resolved_pdf_url=last.url,
                    download_method=last.method,
                    downloaded_bytes=last.bytes,
                    download_status="downloaded",
                    download_error_code="",
                    download_error_message="",
                )
                return "downloaded"
        status = (
            "unavailable"
            if last.error_code in {"REMOTE_NOT_FOUND", "REMOTE_FORBIDDEN"}
            else "failed"
        )
        self.repository.update(
            paper.id,
            download_status=status,
            download_error_code=last.error_code,
            download_error_message=last.error_message,
        )
        return status
