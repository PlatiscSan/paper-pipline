"""Download value objects."""

from dataclasses import dataclass, field


@dataclass(frozen=True, slots=True)
class Candidate:
    url: str
    method: str
    headers: dict[str, str] = field(default_factory=dict, repr=False)


@dataclass(slots=True)
class DownloadResult:
    path: str = ""
    url: str = ""
    method: str = ""
    bytes: int = 0
    status: str = "failed"
    error_code: str = ""
    error_message: str = ""
