"""Job deduplication (FR-13).

The same role legitimately appears many times: two boards, a reposting with a new
requisition id, an aggregator copy. Four signals, cheapest and most reliable first:

1. **Source identity** — same ``(source, source_job_id)``. Enforced by a unique
   constraint, so this one cannot be got wrong.
2. **Canonical URL** — normalised to drop tracking parameters and case.
3. **Requisition id** — extracted from the description or URL when present.
4. **Company + title + location + description similarity** — the fuzzy fallback,
   and the only one that can produce a false positive, so it needs a high bar.

Grouping is transitive within a run: matching any member joins that group.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from urllib.parse import urlsplit, urlunsplit

from packages.schemas.job import Job

#: Query parameters that never identify a posting.
TRACKING_PARAMS = frozenset(
    {
        "utm_source",
        "utm_medium",
        "utm_campaign",
        "utm_term",
        "utm_content",
        "gh_src",
        "gh_jid",
        "ref",
        "source",
        "src",
    }
)

REQUISITION_RE = re.compile(
    r"\b(?:req(?:uisition)?(?:\s*(?:id|no|number|#))?|job\s*id|posting\s*id)\s*[:#-]?\s*"
    r"([A-Z0-9][A-Z0-9-]{2,20})\b",
    re.IGNORECASE,
)

_NON_ALNUM = re.compile(r"[^a-z0-9]+")
_COMPANY_SUFFIXES = (
    " inc",
    " inc.",
    " llc",
    " ltd",
    " limited",
    " corp",
    " corporation",
    " gmbh",
    " pvt",
    " private",
    " plc",
    " co",
    " technologies",
    " technology",
)

#: Shingle overlap needed to call two descriptions the same posting. High, because a
#: false merge hides a real job the candidate never sees.
SIMILARITY_THRESHOLD = 0.82
_SHINGLE_SIZE = 5


def normalize_company(name: str) -> str:
    lowered = f" {name.lower().strip()}"
    for suffix in _COMPANY_SUFFIXES:
        if lowered.endswith(suffix):
            lowered = lowered[: -len(suffix)]
            break
    return _NON_ALNUM.sub(" ", lowered).strip()


def normalize_title(title: str) -> str:
    """Normalise a title, dropping decoration that does not change the role."""
    lowered = title.lower()
    lowered = re.sub(r"\((?:remote|hybrid|onsite|on-site)[^)]*\)", " ", lowered)
    lowered = re.sub(r"\b(?:m/f/d|f/m/d|all genders|w/m/d)\b", " ", lowered)
    return _NON_ALNUM.sub(" ", lowered).strip()


def normalize_location(location: str | None) -> str:
    if not location:
        return ""
    return _NON_ALNUM.sub(" ", location.lower()).strip()


def canonical_url(url: str | None) -> str:
    """Strip tracking parameters, fragments and trailing slashes."""
    if not url:
        return ""
    parts = urlsplit(str(url))
    kept = [
        pair
        for pair in parts.query.split("&")
        if pair and pair.split("=", 1)[0].lower() not in TRACKING_PARAMS
    ]
    path = parts.path.rstrip("/") or "/"
    return urlunsplit(
        (parts.scheme.lower(), parts.netloc.lower(), path, "&".join(sorted(kept)), "")
    )


def requisition_id(job: Job) -> str:
    match = REQUISITION_RE.search(job.description or "")
    if match is not None:
        return match.group(1).upper()
    if job.url:
        url_match = REQUISITION_RE.search(str(job.url))
        if url_match is not None:
            return url_match.group(1).upper()
    return ""


def _shingles(text: str) -> set[str]:
    tokens = _NON_ALNUM.sub(" ", text.lower()).split()
    if len(tokens) < _SHINGLE_SIZE:
        return {" ".join(tokens)} if tokens else set()
    return {" ".join(tokens[i : i + _SHINGLE_SIZE]) for i in range(len(tokens) - _SHINGLE_SIZE + 1)}


def description_similarity(left: str, right: str) -> float:
    """Jaccard overlap of word shingles.

    Shingles rather than a bag of words: two different postings at the same company
    share most individual words but very few five-word sequences.
    """
    left_set, right_set = _shingles(left), _shingles(right)
    if not left_set or not right_set:
        return 0.0
    intersection = len(left_set & right_set)
    union = len(left_set | right_set)
    return intersection / union if union else 0.0


@dataclass(slots=True)
class DedupeKey:
    """Precomputed comparison keys for one job."""

    source: str
    source_job_id: str
    url: str
    requisition: str
    company: str
    title: str
    location: str
    description: str = field(repr=False, default="")

    @classmethod
    def of(cls, job: Job) -> DedupeKey:
        return cls(
            source=job.source,
            source_job_id=job.source_job_id,
            url=canonical_url(str(job.url) if job.url else None),
            requisition=requisition_id(job),
            company=normalize_company(job.company),
            title=normalize_title(job.title),
            location=normalize_location(job.location),
            description=job.description or "",
        )

    def group_hash(self) -> str:
        """Stable id for the company/title/location identity of this posting."""
        material = f"{self.company}|{self.title}|{self.location}"
        return hashlib.sha256(material.encode()).hexdigest()[:32]


def is_duplicate(left: DedupeKey, right: DedupeKey) -> tuple[bool, str]:
    """Decide whether two jobs are the same posting, and say which signal decided."""
    if left.source == right.source and left.source_job_id == right.source_job_id:
        return True, "source_identity"

    if left.url and left.url == right.url:
        return True, "canonical_url"

    if left.requisition and left.requisition == right.requisition:
        # A requisition id only identifies a posting within one company.
        if left.company == right.company:
            return True, "requisition_id"

    if (
        left.company == right.company
        and left.title == right.title
        and left.location == right.location
    ):
        similarity = description_similarity(left.description, right.description)
        if similarity >= SIMILARITY_THRESHOLD:
            return True, f"description_similarity={similarity:.2f}"

    return False, ""


def assign_groups(jobs: list[Job]) -> dict[str, str]:
    """Map each job id to a dedupe group id.

    Returns one entry per job. Jobs judged the same posting share a group id, taken
    from the first member seen so the result is stable for a given input order.
    """
    keys = {job.id: DedupeKey.of(job) for job in jobs}
    groups: dict[str, str] = {}
    representatives: list[tuple[str, DedupeKey]] = []

    for job in jobs:
        key = keys[job.id]
        for representative_id, representative_key in representatives:
            duplicate, _reason = is_duplicate(key, representative_key)
            if duplicate:
                groups[job.id] = groups[representative_id]
                break
        else:
            groups[job.id] = key.group_hash()
            representatives.append((job.id, key))

    return groups
