"""Resumable asynchronous HTTP downloader."""

import asyncio
import email.utils
import hashlib
import os
import random
from collections import defaultdict
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlsplit

import aiohttp
from paper_pipeline.download.models import Candidate, DownloadResult
from paper_pipeline.download.storage import is_pdf


class DownloadClient:
    def __init__(
        self,
        session: aiohttp.ClientSession,
        retries: int = 3,
        delay: float = 0.2,
        max_size_mb: int = 100,
    ) -> None:
        self.session, self.retries, self.delay = session, retries, delay
        self.max_bytes = max_size_mb * 1024 * 1024
        self._locks: defaultdict[str, asyncio.Lock] = defaultdict(asyncio.Lock)
        self._last: defaultdict[str, float] = defaultdict(float)

    async def fetch(self, candidate: Candidate, target: Path) -> DownloadResult:
        digest = hashlib.sha256(candidate.url.encode()).hexdigest()[:12]
        part = target.with_suffix(f".{digest}.part")
        host = urlsplit(candidate.url).netloc
        for attempt in range(self.retries + 1):
            try:
                async with self._locks[host]:
                    wait = self.delay - (asyncio.get_running_loop().time() - self._last[host])
                    if wait > 0:
                        await asyncio.sleep(wait)
                    result = await self._request(candidate, target, part)
                    self._last[host] = asyncio.get_running_loop().time()
                if result.status == "retry" and attempt < self.retries:
                    await asyncio.sleep(result.bytes or (2**attempt + random.random()))
                    continue
                return result
            except (TimeoutError, aiohttp.ServerTimeoutError) as exc:
                if attempt == self.retries:
                    return DownloadResult(
                        status="failed", error_code="NETWORK_TIMEOUT", error_message=str(exc)
                    )
                await asyncio.sleep(2**attempt + random.random())
            except aiohttp.ClientError as exc:
                return DownloadResult(
                    status="failed", error_code="NETWORK_ERROR", error_message=str(exc)
                )
        return DownloadResult(status="failed")

    async def _request(self, candidate: Candidate, target: Path, part: Path) -> DownloadResult:
        offset = part.stat().st_size if part.exists() else 0
        headers = dict(candidate.headers)
        if offset:
            headers["Range"] = f"bytes={offset}-"
        async with self.session.get(candidate.url, headers=headers) as response:
            if response.status in {429, 500, 502, 503, 504}:
                return DownloadResult(
                    status="retry", bytes=int(_retry_after(response.headers.get("Retry-After")))
                )
            if response.status == 403:
                return DownloadResult(
                    status="failed",
                    error_code="REMOTE_FORBIDDEN",
                    error_message="remote refused access",
                )
            if response.status == 404:
                return DownloadResult(
                    status="failed",
                    error_code="REMOTE_NOT_FOUND",
                    error_message="candidate not found",
                )
            if response.status not in {200, 206}:
                return DownloadResult(
                    status="failed",
                    error_code="HTTP_ERROR",
                    error_message=f"HTTP {response.status}",
                )
            if offset and response.status == 200:
                offset = 0
            length = int(response.headers.get("Content-Length", 0)) + offset
            if length > self.max_bytes:
                return DownloadResult(
                    status="failed",
                    error_code="FILE_TOO_LARGE",
                    error_message="size limit exceeded",
                )
            target.parent.mkdir(parents=True, exist_ok=True)
            mode = "ab" if offset else "wb"
            with part.open(mode) as handle:
                async for chunk in response.content.iter_chunked(65536):
                    handle.write(chunk)
                    if handle.tell() > self.max_bytes:
                        return DownloadResult(
                            status="failed",
                            error_code="FILE_TOO_LARGE",
                            error_message="size limit exceeded",
                        )
        if not is_pdf(part):
            with part.open("rb") as handle:
                prefix = handle.read(512).lower()
            part.unlink(missing_ok=True)
            code = "HTML_INSTEAD_OF_PDF" if b"<html" in prefix else "INVALID_PDF"
            return DownloadResult(
                status="failed", error_code=code, error_message="response is not a PDF"
            )
        os.replace(part, target)
        return DownloadResult(
            path=str(target),
            url=candidate.url,
            method=candidate.method,
            bytes=target.stat().st_size,
            status="downloaded",
        )


def _retry_after(value: str | None) -> float:
    if not value:
        return 0
    try:
        return max(0, float(value))
    except ValueError:
        date = email.utils.parsedate_to_datetime(value)
        return max(0, (date - datetime.now(UTC)).total_seconds())
