"""Read-only probing of supported-ATS boards for lead resolution (Phase 5.7).

Uses the same documented public endpoints the adapters use (their API_BASE constants), one
request per probe, no retries, a hard per-run request budget shared by all platforms, and a
full stop per platform on HTTP 429. It never fetches a posting page, an application page or
anything on an employer's own website.
"""

import re
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

import httpx2 as httpx

from integrations.job_sources.ashby import API_BASE as ASHBY_API
from integrations.job_sources.base import SourceError, SourceRateLimited
from integrations.job_sources.greenhouse import API_BASE as GREENHOUSE_API
from integrations.job_sources.http import PoliteHttp
from integrations.job_sources.lever import API_BASE as LEVER_API
from integrations.job_sources.smartrecruiters import API_BASE as SMARTRECRUITERS_API

PLATFORMS = ("greenhouse", "smartrecruiters", "ashby", "lever")
_URL = re.compile(r"https?://\S+", re.I)


class ProbeBudgetExhausted(RuntimeError):
    """The per-run resolution budget is used up."""


class CompanyBudgetExhausted(RuntimeError):
    """This company's share of the budget is used up (the run goes on with the next one)."""


@dataclass
class BoardProbe:
    """What a board's public API says about itself."""

    platform: str
    board_id: str  # the exact identifier the adapter must use
    name: str | None = None  # board / company name, when the API returns one
    postings: int = 0
    texts: list[str] = field(default_factory=list)  # posting descriptions, URLs removed


class AtsProbe:
    def __init__(
        self,
        *,
        user_agent: str,
        max_requests: int,
        delay_seconds: float = 1.0,
        client: httpx.Client | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.max_requests = max_requests
        self.http = {
            p: PoliteHttp(
                f"{p} (lead resolution)",
                user_agent=user_agent,
                delay_seconds=delay_seconds,
                max_attempts=1,
                client=client,
                sleep=sleep,
            )
            for p in PLATFORMS
        }
        self.stopped: set[str] = set()  # platforms that answered 429 this run
        self.errors: list[str] = []
        # Every (platform, board id) answer of this run, found or not: never asked twice.
        self._seen: dict[tuple[str, str], BoardProbe | None] = {}
        self.cache_hits = 0
        self._company_limit: int | None = None
        # Definite misses (the API answered: no such board). Errors, timeouts and 429s are
        # never misses, so they are retried later as usual.
        self.misses: set[tuple[str, str]] = set()
        self._known_misses: set[tuple[str, str]] = set()  # proven in an earlier run
        self.miss_cache_hits = 0

    def preload_misses(self, misses: set[tuple[str, str]]) -> None:
        """Misses proven within the negative-cache window: not asked again, for any company."""
        self._known_misses = {(p, b.lower()) for p, b in misses}

    def is_miss(self, platform: str, board_id: str) -> bool:
        key = (platform, board_id.lower())
        return key in self.misses or key in self._known_misses

    def start_company(self, max_requests: int) -> None:
        """Cap the requests the next company may use (on top of the run budget)."""
        self._company_limit = self.requests_used + max_requests

    @property
    def requests_used(self) -> int:
        return sum(h.requests for h in self.http.values())

    def _get(self, platform: str, url: str, params: dict[str, Any] | None = None) -> Any | None:
        if platform in self.stopped:
            return None
        if self.requests_used >= self.max_requests:
            raise ProbeBudgetExhausted(f"lead-resolution budget ({self.max_requests}) used up")
        if self._company_limit is not None and self.requests_used >= self._company_limit:
            raise CompanyBudgetExhausted("per-company resolution budget used up")
        try:
            return self.http[platform].get_json(url, params=params)
        except SourceRateLimited as exc:
            self.stopped.add(platform)
            self.errors.append(str(exc))
            return None
        except SourceError as exc:
            self.errors.append(str(exc))
            return None

    def probe(self, platform: str, board_id: str) -> BoardProbe | None:
        """The board if it exists and lists at least one posting, else None. Each (platform,
        board id) is asked at most once per run; a platform stopped by 429 is not asked."""
        key = (platform, board_id.lower())
        if key in self._known_misses:
            self.miss_cache_hits += 1
            return None
        if key in self._seen:
            self.cache_hits += 1
            return self._seen[key]
        if platform in self.stopped:
            return None
        before = len(self.errors)
        result = self._probe(platform, board_id)
        if platform not in self.stopped and len(self.errors) == before:
            self._seen[key] = result  # only definite answers are remembered
            if result is None:
                self.misses.add(key)
        return result

    def _probe(self, platform: str, board_id: str) -> BoardProbe | None:
        if platform == "greenhouse":
            meta = self._get(platform, f"{GREENHOUSE_API}/{board_id}")
            if not isinstance(meta, dict) or not meta.get("name"):
                return None
            return BoardProbe(platform, board_id, name=str(meta["name"]))
        if platform == "smartrecruiters":
            data = self._get(platform, f"{SMARTRECRUITERS_API}/{board_id}/postings", {"limit": 10})
            content = (data or {}).get("content") if isinstance(data, dict) else None
            if not isinstance(content, list) or not content:
                return None
            company = (content[0] or {}).get("company") or {}
            return BoardProbe(
                platform,
                str(company.get("identifier") or board_id),
                name=company.get("name"),
                postings=int((data or {}).get("totalFound") or len(content)),
            )
        if platform == "lever":
            data = self._get(platform, f"{LEVER_API}/{board_id}", {"mode": "json", "limit": 20})
            if not isinstance(data, list) or not data:
                return None
            texts = [_text(j.get("descriptionPlain"), j.get("additionalPlain")) for j in data]
            return BoardProbe(platform, board_id, postings=len(data), texts=texts)
        if platform == "ashby":
            data = self._get(platform, f"{ASHBY_API}/{board_id}")
            jobs = (data or {}).get("jobs") if isinstance(data, dict) else None
            if not isinstance(jobs, list) or not jobs:
                return None
            texts = [_text(j.get("descriptionPlain")) for j in jobs[:20]]
            return BoardProbe(platform, board_id, postings=len(jobs), texts=texts)
        raise ValueError(f"Unsupported platform {platform!r}")

    def close(self) -> None:
        for http in self.http.values():
            http.close()


def _text(*parts: Any) -> str:
    return _URL.sub(" ", " ".join(str(p) for p in parts if p))


def mention_ratio(texts: list[str], company: str) -> float:
    """Share of posting texts that name the company (whole words, URLs already removed)."""
    tokens = re.findall(r"[a-z0-9]+", company.lower())
    if not texts or not tokens:
        return 0.0
    pattern = re.compile(r"\b" + r"[\s.\-]*".join(map(re.escape, tokens)) + r"\b", re.I)
    return sum(1 for t in texts if pattern.search(t)) / len(texts)
