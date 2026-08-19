"""Job source adapters.

* :class:`LocalFixtureSource` — reads postings from a local JSON file. This is the
  default and the one tests use: no network, fully deterministic, and it lets the
  whole discovery/matching/tailoring path be developed before any integration.
* :class:`GreenhouseSource`, :class:`LeverSource`, :class:`AshbySource`,
  :class:`SmartRecruitersSource` — public, documented per-company job board APIs.
  Each serves the same JSON a company's own careers page consumes, needs no
  credentials, and is the kind of official interface the PRD says to prefer over
  scraping.
* :class:`AdzunaSource` — a public aggregator API (free self-serve key, no
  partnership review) covering many boards per query, with an India region.
* :class:`ArbeitnowSource` — a fully public aggregator API, no key at all.
* :class:`CareerPageSource` — arbitrary public company career pages, read via the
  schema.org ``JobPosting`` structured data most of them already embed for
  Google/Bing job search crawlers. This is the one adapter here that isn't a
  documented API: it reads only that structured, machine-readable markup (never
  guesses at a page's visual layout), always checks ``robots.txt`` first, and
  follows only a bounded number of same-domain links.

Deliberately absent, everywhere in this module: logging in, solving a CAPTCHA, or
otherwise working around a technical access control. That is a product boundary,
not a missing feature.
"""

from __future__ import annotations

import json
import re
import time
import urllib.robotparser
from datetime import UTC, datetime
from html.parser import HTMLParser
from pathlib import Path
from typing import Any
from urllib.parse import urljoin, urlparse

import httpx

from packages.core.errors import DomainError
from packages.core.ids import new_id
from packages.core.logging import get_logger
from packages.schemas.enums import EmploymentType, RemoteMode
from packages.schemas.job import Job, JobSearchCriteria, SalaryRange, SourceHealth
from services.jobs.base import utcnow

logger = get_logger(__name__)

_HTML_TAG = re.compile(r"<[^>]+>")
_HTML_ENTITIES = {
    "&amp;": "&",
    "&lt;": "<",
    "&gt;": ">",
    "&quot;": '"',
    "&#39;": "'",
    "&nbsp;": " ",
    "&mdash;": "—",
    "&ndash;": "–",  # noqa: RUF001 - decoding the entity to its real character
}

_REMOTE_HINTS = (
    ("hybrid", RemoteMode.HYBRID),
    ("remote", RemoteMode.REMOTE),
    ("on-site", RemoteMode.ONSITE),
    ("onsite", RemoteMode.ONSITE),
    ("in office", RemoteMode.ONSITE),
)

_EMPLOYMENT_HINTS = (
    ("intern|internship", EmploymentType.INTERNSHIP),
    ("contract", EmploymentType.CONTRACT),
    ("part-time|part time", EmploymentType.PART_TIME),
    ("temporary", EmploymentType.TEMPORARY),
    ("full-time|full time", EmploymentType.FULL_TIME),
)


def html_to_text(html: str) -> str:
    """Strip tags and decode the entities job boards actually emit.

    Job descriptions are untrusted third-party content. Converting to plain text
    here means no markup reaches a prompt or the UI, and the text still travels
    fenced as ``UntrustedContent`` downstream.
    """
    text = html.replace("</p>", "\n").replace("<br>", "\n").replace("<br/>", "\n")
    text = text.replace("</li>", "\n").replace("<li>", "- ")
    text = _HTML_TAG.sub(" ", text)
    for entity, char in _HTML_ENTITIES.items():
        text = text.replace(entity, char)
    lines = [re.sub(r"[ \t]+", " ", line).strip() for line in text.splitlines()]
    return "\n".join(line for line in lines if line)


def infer_remote_mode(*texts: str) -> RemoteMode:
    haystack = " ".join(texts).lower()
    for hint, mode in _REMOTE_HINTS:
        if hint in haystack:
            return mode
    return RemoteMode.UNKNOWN


def infer_employment_type(*texts: str) -> EmploymentType:
    """Word-boundary match: a plain substring check would call an "international
    compliance" job an internship, since "intern" is a substring of "international".
    """
    haystack = " ".join(texts).lower()
    for hint, employment in _EMPLOYMENT_HINTS:
        if re.search(rf"\b(?:{hint})\b", haystack):
            return employment
    return EmploymentType.UNKNOWN


def matches_criteria(job: Job, criteria: JobSearchCriteria) -> bool:
    """Apply criteria a source could not filter on server-side.

    Kept permissive on purpose: discovery should over-return and let scoring rank.
    A hard filter that silently drops a good job is worse than a low-ranked one.
    """
    if criteria.titles:
        title = job.title.lower()
        if not any(wanted.lower() in title for wanted in criteria.titles):
            return False

    if criteria.remote_modes and job.remote is not RemoteMode.UNKNOWN:
        if job.remote not in criteria.remote_modes:
            return False

    if criteria.employment_types and job.employment_type is not EmploymentType.UNKNOWN:
        if job.employment_type not in criteria.employment_types:
            return False

    if criteria.locations and job.location:
        location = job.location.lower()
        remote_ok = job.remote is RemoteMode.REMOTE
        if not remote_ok and not any(w.lower() in location for w in criteria.locations):
            return False

    if criteria.keywords:
        blob = f"{job.title}\n{job.description}".lower()
        if not any(keyword.lower() in blob for keyword in criteria.keywords):
            return False

    # An unknown salary is not a rejection: absent data never becomes a negative.
    if criteria.min_salary is not None and job.salary and job.salary.max_amount:
        if job.salary.max_amount < criteria.min_salary:
            return False

    return True


class LocalFixtureSource:
    """Postings from a local JSON file. Offline, deterministic, always available."""

    name = "local"

    def __init__(self, path: Path) -> None:
        self._path = path

    def _load(self) -> list[dict[str, Any]]:
        if not self._path.is_file():
            return []
        payload = json.loads(self._path.read_text(encoding="utf-8"))
        if not isinstance(payload, list):
            raise DomainError(f"{self._path} must contain a JSON array of postings")
        return [item for item in payload if isinstance(item, dict)]

    def normalize(self, raw: dict[str, Any]) -> Job:
        description = str(raw.get("description", ""))
        location = raw.get("location")
        salary_raw = raw.get("salary") or {}

        return Job(
            id=new_id("job"),
            source=self.name,
            source_job_id=str(raw["source_job_id"]),
            company=str(raw.get("company", "Unknown")),
            title=str(raw.get("title", "Unknown")),
            location=str(location) if location else None,
            remote=(
                RemoteMode(raw["remote"])
                if raw.get("remote")
                else infer_remote_mode(description, str(location or ""))
            ),
            employment_type=(
                EmploymentType(raw["employment_type"])
                if raw.get("employment_type")
                else infer_employment_type(description)
            ),
            description=description,
            salary=SalaryRange(**salary_raw) if salary_raw else None,
            url=raw.get("url"),
            retrieved_at=utcnow(),
            raw=raw,
        )

    def search(self, criteria: JobSearchCriteria) -> list[Job]:
        jobs = [self.normalize(raw) for raw in self._load()]
        selected = [job for job in jobs if matches_criteria(job, criteria)]
        return selected[: criteria.limit]

    def fetch(self, source_job_id: str) -> Job | None:
        for raw in self._load():
            if str(raw.get("source_job_id")) == source_job_id:
                return self.normalize(raw)
        return None

    def health_check(self) -> SourceHealth:
        exists = self._path.is_file()
        return SourceHealth(
            source=self.name,
            healthy=exists,
            detail=str(self._path) if exists else f"fixture file missing: {self._path}",
            checked_at=utcnow(),
        )


class GreenhouseSource:
    """The public Greenhouse job board API.

    One board token per company, exactly as a public careers page uses. Requests are
    serialised with a small delay: this is a courtesy interface, so the adapter stays
    well under any rate limit rather than probing for one.
    """

    name = "greenhouse"
    base_url = "https://boards-api.greenhouse.io/v1/boards"

    def __init__(
        self,
        board_tokens: list[str],
        client: httpx.Client | None = None,
        request_delay_seconds: float = 0.5,
    ) -> None:
        self._board_tokens = board_tokens
        self._client = client or httpx.Client(
            timeout=20.0,
            headers={"User-Agent": "career-agent/0.1 (local, personal use)"},
            follow_redirects=True,
        )
        self._delay = request_delay_seconds

    def normalize(self, raw: dict[str, Any], board_token: str) -> Job:
        content = html_to_text(str(raw.get("content", "")))
        location = (raw.get("location") or {}).get("name")
        metadata = " ".join(
            str(item.get("value", "")) for item in (raw.get("metadata") or []) if item
        )

        return Job(
            id=new_id("job"),
            source=self.name,
            source_job_id=str(raw["id"]),
            company=str(raw.get("company_name") or board_token),
            title=str(raw.get("title", "Unknown")),
            location=str(location) if location else None,
            remote=infer_remote_mode(str(location or ""), content, metadata),
            employment_type=infer_employment_type(content, metadata),
            description=content,
            url=raw.get("absolute_url"),
            posted_at=None,
            retrieved_at=utcnow(),
            raw={"board_token": board_token, "id": raw.get("id")},
        )

    def _get(self, url: str) -> dict[str, Any]:
        response = self._client.get(url)
        response.raise_for_status()
        payload: dict[str, Any] = response.json()
        return payload

    def search(self, criteria: JobSearchCriteria) -> list[Job]:
        collected: list[Job] = []

        for index, token in enumerate(self._board_tokens):
            if index:
                time.sleep(self._delay)
            try:
                payload = self._get(f"{self.base_url}/{token}/jobs?content=true")
            except httpx.HTTPError as exc:
                # One unreachable board must not fail the whole discovery run.
                logger.warning(
                    "jobs.source_unavailable",
                    extra={"source": self.name, "board": token, "error": type(exc).__name__},
                )
                continue

            for raw in payload.get("jobs", []):
                job = self.normalize(raw, token)
                if matches_criteria(job, criteria):
                    collected.append(job)
                if len(collected) >= criteria.limit:
                    return collected

        return collected

    def fetch(self, source_job_id: str) -> Job | None:
        for token in self._board_tokens:
            try:
                raw = self._get(f"{self.base_url}/{token}/jobs/{source_job_id}?questions=false")
            except httpx.HTTPError:
                continue
            if raw.get("id"):
                return self.normalize(raw, token)
        return None

    def health_check(self) -> SourceHealth:
        if not self._board_tokens:
            return SourceHealth(
                source=self.name,
                healthy=False,
                detail="no board tokens configured (CA_GREENHOUSE_BOARDS)",
                checked_at=utcnow(),
            )
        token = self._board_tokens[0]
        try:
            self._get(f"{self.base_url}/{token}/jobs")
        except httpx.HTTPError as exc:
            return SourceHealth(
                source=self.name,
                healthy=False,
                detail=f"{type(exc).__name__} contacting board {token}",
                checked_at=utcnow(),
            )
        return SourceHealth(
            source=self.name,
            healthy=True,
            detail=f"{len(self._board_tokens)} board(s) configured",
            checked_at=utcnow(),
        )


class LeverSource:
    """The public Lever job board API (``api.lever.co``).

    Same shape and courtesy pacing as :class:`GreenhouseSource`: one request per
    configured site, serialised with a small delay.
    """

    name = "lever"
    base_url = "https://api.lever.co/v0/postings"

    def __init__(
        self,
        sites: list[str],
        client: httpx.Client | None = None,
        request_delay_seconds: float = 0.5,
    ) -> None:
        self._sites = sites
        self._client = client or httpx.Client(
            timeout=20.0,
            headers={"User-Agent": "career-agent/0.1 (local, personal use)"},
            follow_redirects=True,
        )
        self._delay = request_delay_seconds

    def normalize(self, raw: dict[str, Any], site: str) -> Job:
        categories = raw.get("categories") or {}
        location = categories.get("location")
        lists_text = " ".join(
            html_to_text(str(item.get("content", ""))) for item in (raw.get("lists") or []) if item
        )
        description = "\n\n".join(
            part
            for part in (
                str(raw.get("descriptionPlain") or html_to_text(str(raw.get("description", "")))),
                lists_text,
            )
            if part
        )
        posted_at = None
        created_at = raw.get("createdAt")
        if isinstance(created_at, (int, float)):
            posted_at = datetime.fromtimestamp(created_at / 1000, tz=UTC)

        # Lever states remote/hybrid/onsite explicitly; only fall back to inference
        # when it reports "unspecified".
        workplace_type = str(raw.get("workplaceType", ""))
        remote = (
            RemoteMode(workplace_type)
            if workplace_type in {mode.value for mode in RemoteMode}
            else infer_remote_mode(str(location or ""), description)
        )

        return Job(
            id=new_id("job"),
            source=self.name,
            source_job_id=str(raw["id"]),
            company=site,
            title=str(raw.get("text", "Unknown")),
            location=str(location) if location else None,
            remote=remote,
            employment_type=infer_employment_type(
                str(categories.get("commitment", "")), description
            ),
            description=description,
            url=raw.get("hostedUrl") or raw.get("applyUrl"),
            posted_at=posted_at,
            retrieved_at=utcnow(),
            raw={"site": site, "id": raw.get("id")},
        )

    def _get(self, url: str) -> Any:
        response = self._client.get(url, params={"mode": "json"})
        response.raise_for_status()
        return response.json()

    def search(self, criteria: JobSearchCriteria) -> list[Job]:
        collected: list[Job] = []

        for index, site in enumerate(self._sites):
            if index:
                time.sleep(self._delay)
            try:
                payload = self._get(f"{self.base_url}/{site}")
            except httpx.HTTPError as exc:
                logger.warning(
                    "jobs.source_unavailable",
                    extra={"source": self.name, "site": site, "error": type(exc).__name__},
                )
                continue

            for raw in payload if isinstance(payload, list) else []:
                job = self.normalize(raw, site)
                if matches_criteria(job, criteria):
                    collected.append(job)
                if len(collected) >= criteria.limit:
                    return collected

        return collected

    def fetch(self, source_job_id: str) -> Job | None:
        for site in self._sites:
            try:
                raw = self._get(f"{self.base_url}/{site}/{source_job_id}")
            except httpx.HTTPError:
                continue
            if isinstance(raw, dict) and raw.get("id"):
                return self.normalize(raw, site)
        return None

    def health_check(self) -> SourceHealth:
        if not self._sites:
            return SourceHealth(
                source=self.name,
                healthy=False,
                detail="no sites configured (CA_LEVER_SITES)",
                checked_at=utcnow(),
            )
        site = self._sites[0]
        try:
            self._get(f"{self.base_url}/{site}")
        except httpx.HTTPError as exc:
            return SourceHealth(
                source=self.name,
                healthy=False,
                detail=f"{type(exc).__name__} contacting site {site}",
                checked_at=utcnow(),
            )
        return SourceHealth(
            source=self.name,
            healthy=True,
            detail=f"{len(self._sites)} site(s) configured",
            checked_at=utcnow(),
        )


_ASHBY_EMPLOYMENT = {
    "fulltime": EmploymentType.FULL_TIME,
    "parttime": EmploymentType.PART_TIME,
    "intern": EmploymentType.INTERNSHIP,
    "internship": EmploymentType.INTERNSHIP,
    "contract": EmploymentType.CONTRACT,
    "temporary": EmploymentType.TEMPORARY,
}

_ASHBY_WORKPLACE = {
    "remote": RemoteMode.REMOTE,
    "hybrid": RemoteMode.HYBRID,
    "onsite": RemoteMode.ONSITE,
}


class AshbySource:
    """The public Ashby Job Board API (``api.ashbyhq.com/posting-api``).

    No single-posting-by-id endpoint is documented, so ``fetch`` re-lists the
    board and filters locally rather than guessing at an undocumented URL.
    """

    name = "ashby"
    base_url = "https://api.ashbyhq.com/posting-api/job-board"

    def __init__(
        self,
        boards: list[str],
        client: httpx.Client | None = None,
        request_delay_seconds: float = 0.5,
    ) -> None:
        self._boards = boards
        self._client = client or httpx.Client(
            timeout=20.0,
            headers={"User-Agent": "career-agent/0.1 (local, personal use)"},
            follow_redirects=True,
        )
        self._delay = request_delay_seconds

    def normalize(self, raw: dict[str, Any], board: str) -> Job:
        location = raw.get("location")
        description = str(
            raw.get("descriptionPlain") or html_to_text(str(raw.get("descriptionHtml", "")))
        )
        employment_type = _ASHBY_EMPLOYMENT.get(
            str(raw.get("employmentType", "")).lower(), EmploymentType.UNKNOWN
        )
        if employment_type is EmploymentType.UNKNOWN:
            employment_type = infer_employment_type(description)

        posted_at = None
        published_at = raw.get("publishedAt")
        if isinstance(published_at, str) and published_at:
            try:
                posted_at = datetime.fromisoformat(published_at.replace("Z", "+00:00"))
            except ValueError:
                posted_at = None

        # Ashby states the workplace type explicitly ("Hybrid" | "OnSite" | "Remote");
        # only fall back to inference if that field is absent.
        workplace_type = str(raw.get("workplaceType", "")).lower()
        remote = _ASHBY_WORKPLACE.get(workplace_type)
        if remote is None:
            remote = (
                RemoteMode.REMOTE
                if raw.get("isRemote")
                else infer_remote_mode(str(location or ""), description)
            )

        return Job(
            id=new_id("job"),
            source=self.name,
            source_job_id=str(raw["id"]),
            company=board,
            title=str(raw.get("title", "Unknown")),
            location=str(location) if location else None,
            remote=remote,
            employment_type=employment_type,
            description=description,
            url=raw.get("jobUrl") or raw.get("applyUrl"),
            posted_at=posted_at,
            retrieved_at=utcnow(),
            raw={"board": board, "id": raw.get("id")},
        )

    def _get(self, board: str) -> dict[str, Any]:
        response = self._client.get(f"{self.base_url}/{board}")
        response.raise_for_status()
        payload: dict[str, Any] = response.json()
        return payload

    def search(self, criteria: JobSearchCriteria) -> list[Job]:
        collected: list[Job] = []

        for index, board in enumerate(self._boards):
            if index:
                time.sleep(self._delay)
            try:
                payload = self._get(board)
            except httpx.HTTPError as exc:
                logger.warning(
                    "jobs.source_unavailable",
                    extra={"source": self.name, "board": board, "error": type(exc).__name__},
                )
                continue

            for raw in payload.get("jobs", []):
                job = self.normalize(raw, board)
                if matches_criteria(job, criteria):
                    collected.append(job)
                if len(collected) >= criteria.limit:
                    return collected

        return collected

    def fetch(self, source_job_id: str) -> Job | None:
        for board in self._boards:
            try:
                payload = self._get(board)
            except httpx.HTTPError:
                continue
            for raw in payload.get("jobs", []):
                if str(raw.get("id")) == source_job_id:
                    return self.normalize(raw, board)
        return None

    def health_check(self) -> SourceHealth:
        if not self._boards:
            return SourceHealth(
                source=self.name,
                healthy=False,
                detail="no boards configured (CA_ASHBY_BOARDS)",
                checked_at=utcnow(),
            )
        board = self._boards[0]
        try:
            self._get(board)
        except httpx.HTTPError as exc:
            return SourceHealth(
                source=self.name,
                healthy=False,
                detail=f"{type(exc).__name__} contacting board {board}",
                checked_at=utcnow(),
            )
        return SourceHealth(
            source=self.name,
            healthy=True,
            detail=f"{len(self._boards)} board(s) configured",
            checked_at=utcnow(),
        )


class SmartRecruitersSource:
    """The public SmartRecruiters Postings API (``api.smartrecruiters.com``).

    The listing endpoint returns summaries only; the full job ad text needs one
    follow-up request per posting, so this adapter is deliberately paced (a delay
    per request, not just per company) — it is a courtesy interface, not a bulk
    export.
    """

    name = "smartrecruiters"
    base_url = "https://api.smartrecruiters.com/v1/companies"

    def __init__(
        self,
        companies: list[str],
        client: httpx.Client | None = None,
        request_delay_seconds: float = 0.5,
    ) -> None:
        self._companies = companies
        self._client = client or httpx.Client(
            timeout=20.0,
            headers={"User-Agent": "career-agent/0.1 (local, personal use)"},
            follow_redirects=True,
        )
        self._delay = request_delay_seconds

    def normalize(self, summary: dict[str, Any], detail: dict[str, Any], company: str) -> Job:
        location = summary.get("location") or {}
        location_text = ", ".join(
            str(location[key]) for key in ("city", "region", "country") if location.get(key)
        )
        sections = (detail.get("jobAd") or {}).get("sections") or {}
        description = "\n\n".join(
            html_to_text(str(section.get("text", "")))
            for section in sections.values()
            if isinstance(section, dict) and section.get("text")
        )
        posted_at = None
        released = summary.get("releasedDate")
        if isinstance(released, str) and released:
            try:
                posted_at = datetime.fromisoformat(released.replace("Z", "+00:00"))
            except ValueError:
                posted_at = None

        posting_id = str(summary.get("id"))
        if location.get("hybrid"):
            remote = RemoteMode.HYBRID
        elif location.get("remote"):
            remote = RemoteMode.REMOTE
        else:
            remote = infer_remote_mode(location_text, description)

        employment_label = str((summary.get("typeOfEmployment") or {}).get("label", ""))
        employment_type = infer_employment_type(employment_label, description)

        # The API returns the real, slugged apply link; a hand-built URL would
        # 404 (SmartRecruiters appends a title slug the summary doesn't carry).
        url = detail.get("postingUrl") or f"https://jobs.smartrecruiters.com/{company}/{posting_id}"

        return Job(
            id=new_id("job"),
            source=self.name,
            source_job_id=posting_id,
            company=str((summary.get("company") or {}).get("name") or company),
            title=str(summary.get("name", "Unknown")),
            location=location_text or None,
            remote=remote,
            employment_type=employment_type,
            description=description,
            url=url,
            posted_at=posted_at,
            retrieved_at=utcnow(),
            raw={"company": company, "id": posting_id},
        )

    def _get(self, url: str) -> dict[str, Any]:
        response = self._client.get(url)
        response.raise_for_status()
        payload: dict[str, Any] = response.json()
        return payload

    def search(self, criteria: JobSearchCriteria) -> list[Job]:
        collected: list[Job] = []

        for index, company in enumerate(self._companies):
            if index:
                time.sleep(self._delay)
            try:
                payload = self._get(f"{self.base_url}/{company}/postings?limit=100")
            except httpx.HTTPError as exc:
                logger.warning(
                    "jobs.source_unavailable",
                    extra={"source": self.name, "company": company, "error": type(exc).__name__},
                )
                continue

            for summary in payload.get("content", []):
                posting_id = summary.get("id")
                if posting_id is None:
                    continue
                time.sleep(self._delay)
                try:
                    detail = self._get(f"{self.base_url}/{company}/postings/{posting_id}")
                except httpx.HTTPError:
                    detail = {}
                job = self.normalize(summary, detail, company)
                if matches_criteria(job, criteria):
                    collected.append(job)
                if len(collected) >= criteria.limit:
                    return collected

        return collected

    def fetch(self, source_job_id: str) -> Job | None:
        for company in self._companies:
            try:
                detail = self._get(f"{self.base_url}/{company}/postings/{source_job_id}")
            except httpx.HTTPError:
                continue
            if detail.get("id"):
                summary = {
                    "id": detail.get("id"),
                    "name": detail.get("name"),
                    "location": detail.get("location"),
                    "company": detail.get("company"),
                    "releasedDate": detail.get("releasedDate"),
                    "typeOfEmployment": detail.get("typeOfEmployment"),
                }
                return self.normalize(summary, detail, company)
        return None

    def health_check(self) -> SourceHealth:
        if not self._companies:
            return SourceHealth(
                source=self.name,
                healthy=False,
                detail="no companies configured (CA_SMARTRECRUITERS_COMPANIES)",
                checked_at=utcnow(),
            )
        company = self._companies[0]
        try:
            self._get(f"{self.base_url}/{company}/postings?limit=1")
        except httpx.HTTPError as exc:
            return SourceHealth(
                source=self.name,
                healthy=False,
                detail=f"{type(exc).__name__} contacting company {company}",
                checked_at=utcnow(),
            )
        return SourceHealth(
            source=self.name,
            healthy=True,
            detail=f"{len(self._companies)} compan{'y' if len(self._companies) == 1 else 'ies'} configured",
            checked_at=utcnow(),
        )


_ADZUNA_CURRENCY = {
    "in": "INR",
    "us": "USD",
    "gb": "GBP",
    "au": "AUD",
    "ca": "CAD",
    "de": "EUR",
    "fr": "EUR",
    "nl": "EUR",
    "it": "EUR",
    "at": "EUR",
    "be": "EUR",
    "es": "EUR",
    "sg": "SGD",
    "za": "ZAR",
    "nz": "NZD",
    "br": "BRL",
    "mx": "MXN",
    "pl": "PLN",
}


class AdzunaSource:
    """The public Adzuna aggregator API (``api.adzuna.com``).

    A free self-serve key from adzuna.com/apis — no partner review — covers a
    search across many boards for one country per request.

    Adzuna's public API has no lookup-by-id endpoint, so ``fetch`` always
    returns ``None`` rather than faking one.
    """

    name = "adzuna"
    base_url = "https://api.adzuna.com/v1/api/jobs"

    def __init__(
        self,
        app_id: str,
        app_key: str,
        country: str = "in",
        client: httpx.Client | None = None,
        request_delay_seconds: float = 0.5,
    ) -> None:
        self._app_id = app_id
        self._app_key = app_key
        self._country = country
        self._client = client or httpx.Client(
            timeout=20.0,
            headers={"User-Agent": "career-agent/0.1 (local, personal use)"},
            follow_redirects=True,
        )
        self._delay = request_delay_seconds

    def normalize(self, raw: dict[str, Any]) -> Job:
        company = str((raw.get("company") or {}).get("display_name", "Unknown"))
        location = (raw.get("location") or {}).get("display_name")
        description = html_to_text(str(raw.get("description", "")))
        contract_time = str(raw.get("contract_time", "")).replace("_", " ")
        salary = None
        if raw.get("salary_min") or raw.get("salary_max"):
            salary = SalaryRange(
                min_amount=int(raw["salary_min"]) if raw.get("salary_min") else None,
                max_amount=int(raw["salary_max"]) if raw.get("salary_max") else None,
                currency=_ADZUNA_CURRENCY.get(self._country),
                period="year",
            )
        posted_at = None
        created = raw.get("created")
        if isinstance(created, str) and created:
            try:
                posted_at = datetime.fromisoformat(created.replace("Z", "+00:00"))
            except ValueError:
                posted_at = None

        title = str(raw.get("title", "Unknown"))
        return Job(
            id=new_id("job"),
            source=self.name,
            source_job_id=str(raw.get("id")),
            company=company,
            title=title,
            location=str(location) if location else None,
            remote=infer_remote_mode(title, description, str(location or "")),
            employment_type=infer_employment_type(contract_time, description),
            description=description,
            salary=salary,
            url=raw.get("redirect_url"),
            posted_at=posted_at,
            retrieved_at=utcnow(),
            raw={"id": raw.get("id")},
        )

    def _get(self, url: str, params: dict[str, Any]) -> dict[str, Any]:
        response = self._client.get(url, params=params)
        response.raise_for_status()
        payload: dict[str, Any] = response.json()
        return payload

    def _base_params(self, results_per_page: int) -> dict[str, Any]:
        return {
            "app_id": self._app_id,
            "app_key": self._app_key,
            "results_per_page": results_per_page,
            "content-type": "application/json",
        }

    def search(self, criteria: JobSearchCriteria) -> list[Job]:
        what = " ".join(criteria.titles or criteria.keywords)
        locations: list[str | None] = list(criteria.locations) if criteria.locations else [None]
        collected: list[Job] = []

        for index, where in enumerate(locations):
            if index:
                time.sleep(self._delay)
            params = self._base_params(min(criteria.limit, 50))
            if what:
                params["what"] = what
            if where:
                params["where"] = where
            try:
                payload = self._get(f"{self.base_url}/{self._country}/search/1", params)
            except httpx.HTTPError as exc:
                logger.warning(
                    "jobs.source_unavailable",
                    extra={"source": self.name, "where": where, "error": type(exc).__name__},
                )
                continue

            for raw in payload.get("results", []):
                job = self.normalize(raw)
                if matches_criteria(job, criteria):
                    collected.append(job)
                if len(collected) >= criteria.limit:
                    return collected

        return collected

    def fetch(self, source_job_id: str) -> Job | None:
        return None

    def health_check(self) -> SourceHealth:
        try:
            self._get(f"{self.base_url}/{self._country}/search/1", self._base_params(1))
        except httpx.HTTPError as exc:
            return SourceHealth(
                source=self.name,
                healthy=False,
                detail=f"{type(exc).__name__} contacting Adzuna ({self._country})",
                checked_at=utcnow(),
            )
        return SourceHealth(
            source=self.name,
            healthy=True,
            detail=f"country={self._country}",
            checked_at=utcnow(),
        )


class ArbeitnowSource:
    """The public Arbeitnow aggregator API (``arbeitnow.com``). No key needed.

    The API takes no search query parameters, so this pages through the most
    recent postings and lets local filtering (:func:`matches_criteria`) narrow
    the result — over-returning is the documented default (see ``matches_criteria``).
    """

    name = "arbeitnow"
    base_url = "https://www.arbeitnow.com/api/job-board-api"
    _max_pages = 5

    def __init__(
        self,
        client: httpx.Client | None = None,
        request_delay_seconds: float = 0.5,
    ) -> None:
        self._client = client or httpx.Client(
            timeout=20.0,
            headers={"User-Agent": "career-agent/0.1 (local, personal use)"},
            follow_redirects=True,
        )
        self._delay = request_delay_seconds

    def normalize(self, raw: dict[str, Any]) -> Job:
        description = html_to_text(str(raw.get("description", "")))
        job_types = " ".join(str(item) for item in (raw.get("job_types") or []))
        posted_at = None
        created_at = raw.get("created_at")
        if isinstance(created_at, (int, float)):
            posted_at = datetime.fromtimestamp(created_at, tz=UTC)
        location = raw.get("location")

        return Job(
            id=new_id("job"),
            source=self.name,
            source_job_id=str(raw.get("slug")),
            company=str(raw.get("company_name", "Unknown")),
            title=str(raw.get("title", "Unknown")),
            location=str(location) if location else None,
            remote=(
                RemoteMode.REMOTE
                if raw.get("remote")
                else infer_remote_mode(str(location or ""), description)
            ),
            employment_type=infer_employment_type(job_types, description),
            description=description,
            url=raw.get("url"),
            posted_at=posted_at,
            retrieved_at=utcnow(),
            raw={"slug": raw.get("slug")},
        )

    def _get(self, url: str) -> dict[str, Any]:
        response = self._client.get(url)
        response.raise_for_status()
        payload: dict[str, Any] = response.json()
        return payload

    def _pages(self) -> list[dict[str, Any]]:
        pages = []
        url: str | None = self.base_url
        for index in range(self._max_pages):
            if url is None:
                break
            if index:
                time.sleep(self._delay)
            payload = self._get(url)
            pages.append(payload)
            url = (payload.get("links") or {}).get("next")
        return pages

    def search(self, criteria: JobSearchCriteria) -> list[Job]:
        collected: list[Job] = []
        try:
            pages = self._pages()
        except httpx.HTTPError as exc:
            logger.warning(
                "jobs.source_unavailable",
                extra={"source": self.name, "error": type(exc).__name__},
            )
            return collected

        for payload in pages:
            for raw in payload.get("data", []):
                job = self.normalize(raw)
                if matches_criteria(job, criteria):
                    collected.append(job)
                if len(collected) >= criteria.limit:
                    return collected

        return collected

    def fetch(self, source_job_id: str) -> Job | None:
        try:
            pages = self._pages()
        except httpx.HTTPError:
            return None
        for payload in pages:
            for raw in payload.get("data", []):
                if str(raw.get("slug")) == source_job_id:
                    return self.normalize(raw)
        return None

    def health_check(self) -> SourceHealth:
        try:
            payload = self._get(self.base_url)
        except httpx.HTTPError as exc:
            return SourceHealth(
                source=self.name,
                healthy=False,
                detail=f"{type(exc).__name__} contacting Arbeitnow",
                checked_at=utcnow(),
            )
        return SourceHealth(
            source=self.name,
            healthy=True,
            detail=f"{len(payload.get('data', []))} posting(s) on page 1",
            checked_at=utcnow(),
        )


_SCHEMA_EMPLOYMENT = {
    "full_time": EmploymentType.FULL_TIME,
    "part_time": EmploymentType.PART_TIME,
    "contractor": EmploymentType.CONTRACT,
    "contract": EmploymentType.CONTRACT,
    "temporary": EmploymentType.TEMPORARY,
    "intern": EmploymentType.INTERNSHIP,
    "internship": EmploymentType.INTERNSHIP,
}

_JOB_LINK_HINTS = ("job", "career", "position", "opening", "vacan", "role")


class _PageParser(HTMLParser):
    """Pulls JSON-LD script bodies and same-page anchor hrefs. Stdlib only —
    tolerant of the messy, inconsistent HTML real company sites actually ship,
    without a new dependency.
    """

    def __init__(self) -> None:
        super().__init__()
        self.ld_json_blocks: list[str] = []
        self.links: list[str] = []
        self._buffer: list[str] = []
        self._in_ld_json = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attr_dict = dict(attrs)
        if tag == "script" and (attr_dict.get("type") or "").lower() == "application/ld+json":
            self._in_ld_json = True
            self._buffer = []
        elif tag == "a" and attr_dict.get("href"):
            href = attr_dict["href"]
            if href:
                self.links.append(href)

    def handle_endtag(self, tag: str) -> None:
        if tag == "script" and self._in_ld_json:
            self.ld_json_blocks.append("".join(self._buffer))
            self._in_ld_json = False

    def handle_data(self, data: str) -> None:
        if self._in_ld_json:
            self._buffer.append(data)


def _iter_job_postings(blocks: list[str]) -> list[dict[str, Any]]:
    """Every schema.org ``JobPosting`` object found across a page's JSON-LD blocks.

    Handles the shapes real sites actually emit: a single object, a bare array of
    objects, or a ``@graph`` wrapper around either.
    """
    postings: list[dict[str, Any]] = []
    for block in blocks:
        try:
            payload = json.loads(block.strip())
        except (ValueError, TypeError):
            continue
        candidates = payload if isinstance(payload, list) else [payload]
        for candidate in candidates:
            if not isinstance(candidate, dict):
                continue
            graph = candidate.get("@graph")
            nested = graph if isinstance(graph, list) else [candidate]
            for item in nested:
                if isinstance(item, dict) and item.get("@type") == "JobPosting":
                    postings.append(item)
    return postings


class CareerPageSource:
    """Public company career pages, read via embedded schema.org ``JobPosting``
    structured data — the same machine-readable feed sites publish so Google/Bing
    job search can index them, not reverse-engineered page scraping.

    Every fetch — the start page and any link followed from it — is checked
    against that domain's ``robots.txt`` first and skipped if disallowed. Only
    same-domain, job-shaped links are ever followed, bounded to
    ``max_links_per_page`` per configured start page. No login, no JavaScript
    execution, no CAPTCHA solving: a page that needs any of those to reveal its
    postings is simply unreadable to this adapter, by design.
    """

    name = "career_page"

    def __init__(
        self,
        start_urls: list[str],
        client: httpx.Client | None = None,
        request_delay_seconds: float = 0.5,
        max_links_per_page: int = 25,
    ) -> None:
        self._start_urls = start_urls
        self._client = client or httpx.Client(
            timeout=20.0,
            headers={"User-Agent": "career-agent/0.1 (+local, personal use; respects robots.txt)"},
            follow_redirects=True,
        )
        self._delay = request_delay_seconds
        self._max_links = max_links_per_page
        self._robots_cache: dict[str, urllib.robotparser.RobotFileParser] = {}

    def _robots_for(self, url: str) -> urllib.robotparser.RobotFileParser:
        parsed = urlparse(url)
        origin = f"{parsed.scheme}://{parsed.netloc}"
        cached = self._robots_cache.get(origin)
        if cached is not None:
            return cached

        parser = urllib.robotparser.RobotFileParser()
        try:
            response = self._client.get(f"{origin}/robots.txt")
            parser.parse(response.text.splitlines() if response.status_code == 200 else [])
        except httpx.HTTPError:
            parser.parse([])
        self._robots_cache[origin] = parser
        return parser

    def _allowed(self, url: str) -> bool:
        user_agent = self._client.headers.get("User-Agent", "*")
        return self._robots_for(url).can_fetch(user_agent, url)

    def _get_text(self, url: str) -> str | None:
        if not self._allowed(url):
            logger.info("jobs.robots_disallowed", extra={"source": self.name, "url": url})
            return None
        try:
            response = self._client.get(url)
            response.raise_for_status()
        except httpx.HTTPError as exc:
            logger.warning(
                "jobs.source_unavailable",
                extra={"source": self.name, "url": url, "error": type(exc).__name__},
            )
            return None
        return response.text

    def _parse(self, html: str) -> _PageParser:
        parser = _PageParser()
        parser.feed(html)
        return parser

    def _job_links(self, parser: _PageParser, base_url: str) -> list[str]:
        domain = urlparse(base_url).netloc
        seen: set[str] = set()
        links: list[str] = []
        for href in parser.links:
            absolute = urljoin(base_url, href)
            parsed = urlparse(absolute)
            if parsed.netloc != domain or absolute in seen:
                continue
            # Hint against the path/query only: many job boards live on a host
            # literally named "jobs.*", which would make every same-domain link
            # match on hostname alone.
            haystack = f"{parsed.path}?{parsed.query}".lower()
            if not any(hint in haystack for hint in _JOB_LINK_HINTS):
                continue
            seen.add(absolute)
            links.append(absolute)
            if len(links) >= self._max_links:
                break
        return links

    def _postings_from(self, start_url: str) -> list[tuple[dict[str, Any], str]]:
        html = self._get_text(start_url)
        if html is None:
            return []

        parser = self._parse(html)
        direct = _iter_job_postings(parser.ld_json_blocks)
        if direct:
            return [(posting, start_url) for posting in direct]

        # No structured data on the start page itself: treat it as an index and
        # follow job-shaped same-domain links, each independently robots-checked.
        results: list[tuple[dict[str, Any], str]] = []
        for link in self._job_links(parser, start_url):
            time.sleep(self._delay)
            page_html = self._get_text(link)
            if page_html is None:
                continue
            for posting in _iter_job_postings(self._parse(page_html).ld_json_blocks):
                results.append((posting, link))
        return results

    def normalize(self, raw: dict[str, Any], page_url: str) -> Job:
        posting_url = str(raw.get("url") or page_url)
        org = raw.get("hiringOrganization")
        company = str(
            (org.get("name") if isinstance(org, dict) else None) or urlparse(posting_url).netloc
        )
        description = html_to_text(str(raw.get("description", "")))

        location_obj = raw.get("jobLocation")
        if isinstance(location_obj, list):
            location_obj = location_obj[0] if location_obj else None
        address = (location_obj or {}).get("address") if isinstance(location_obj, dict) else None
        address = address if isinstance(address, dict) else {}
        location_text = (
            ", ".join(
                str(address[key])
                for key in ("addressLocality", "addressRegion", "addressCountry")
                if address.get(key)
            )
            or None
        )

        job_location_type = str(raw.get("jobLocationType", "")).upper()
        remote = (
            RemoteMode.REMOTE
            if job_location_type == "TELECOMMUTE"
            else infer_remote_mode(str(location_text or ""), description)
        )

        employment_raw = raw.get("employmentType")
        if isinstance(employment_raw, list):
            employment_raw = employment_raw[0] if employment_raw else ""
        employment_text = str(employment_raw or "")
        employment_key = employment_text.strip().lower().replace("-", "_")
        employment_type = _SCHEMA_EMPLOYMENT.get(employment_key, EmploymentType.UNKNOWN)
        if employment_type is EmploymentType.UNKNOWN:
            # Sites don't always follow Google's exact enum vocabulary here (e.g.
            # Lever's own embed says "Regular Full Time (Salary)"), so the free text
            # is still worth a word-boundary pass rather than being discarded.
            employment_type = infer_employment_type(employment_text, description)

        salary = None
        base_salary = raw.get("baseSalary")
        if isinstance(base_salary, dict):
            value = base_salary.get("value")
            if isinstance(value, dict) and (value.get("minValue") or value.get("maxValue")):
                salary = SalaryRange(
                    min_amount=int(value["minValue"]) if value.get("minValue") else None,
                    max_amount=int(value["maxValue"]) if value.get("maxValue") else None,
                    currency=(
                        str(base_salary["currency"]) if base_salary.get("currency") else None
                    ),
                    period=str(value.get("unitText") or "").lower() or None,
                )

        posted_at = None
        date_posted = raw.get("datePosted")
        if isinstance(date_posted, str) and date_posted:
            try:
                posted_at = datetime.fromisoformat(date_posted.replace("Z", "+00:00"))
            except ValueError:
                posted_at = None

        return Job(
            id=new_id("job"),
            source=self.name,
            source_job_id=posting_url,
            company=company,
            title=str(raw.get("title", "Unknown")),
            location=location_text,
            remote=remote,
            employment_type=employment_type,
            description=description,
            salary=salary,
            url=posting_url,
            posted_at=posted_at,
            retrieved_at=utcnow(),
            raw={"page_url": page_url, "posting_url": posting_url},
        )

    def search(self, criteria: JobSearchCriteria) -> list[Job]:
        collected: list[Job] = []
        for index, start_url in enumerate(self._start_urls):
            if index:
                time.sleep(self._delay)
            for posting, page_url in self._postings_from(start_url):
                job = self.normalize(posting, page_url)
                if matches_criteria(job, criteria):
                    collected.append(job)
                if len(collected) >= criteria.limit:
                    return collected
        return collected

    def fetch(self, source_job_id: str) -> Job | None:
        # This adapter's id *is* the posting's URL (see normalize()) — refetching
        # it is a direct GET, not a lookup against some other endpoint.
        if not source_job_id.startswith(("http://", "https://")):
            return None
        html = self._get_text(source_job_id)
        if html is None:
            return None
        postings = _iter_job_postings(self._parse(html).ld_json_blocks)
        if not postings:
            return None
        return self.normalize(postings[0], source_job_id)

    def health_check(self) -> SourceHealth:
        if not self._start_urls:
            return SourceHealth(
                source=self.name,
                healthy=False,
                detail="no career pages configured (CA_CAREER_PAGES)",
                checked_at=utcnow(),
            )
        url = self._start_urls[0]
        if not self._allowed(url):
            return SourceHealth(
                source=self.name,
                healthy=False,
                detail=f"robots.txt disallows fetching {url}",
                checked_at=utcnow(),
            )
        try:
            self._client.get(url).raise_for_status()
        except httpx.HTTPError as exc:
            return SourceHealth(
                source=self.name,
                healthy=False,
                detail=f"{type(exc).__name__} contacting {url}",
                checked_at=utcnow(),
            )
        return SourceHealth(
            source=self.name,
            healthy=True,
            detail=f"{len(self._start_urls)} career page(s) configured",
            checked_at=utcnow(),
        )
