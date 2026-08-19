"""The ``JobSource`` adapter contract (skills/02, FR-11).

Four methods, deliberately small. Adding a source means implementing this and
registering it — nothing else in the system changes.

Every adapter must obey the product's boundaries: use official or public
interfaces, honour the source's terms and rate limits, and never attempt to defeat
an access control. An adapter that cannot fetch politely does not get written.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Protocol, runtime_checkable

from packages.schemas.job import Job, JobSearchCriteria, SourceHealth


@runtime_checkable
class JobSource(Protocol):
    """A place jobs come from."""

    #: Stable adapter name, stored on every job for provenance.
    name: str

    def search(self, criteria: JobSearchCriteria) -> list[Job]:
        """Return normalized jobs matching ``criteria``."""
        ...

    def fetch(self, source_job_id: str) -> Job | None:
        """Fetch one job by its source-native id, or None if it is gone."""
        ...

    def health_check(self) -> SourceHealth:
        """Report whether this source is currently usable."""
        ...


def utcnow() -> datetime:
    return datetime.now(UTC)


class SourceRegistry:
    """Adapter lookup.

    A registry rather than imports scattered through the service so that which
    sources exist is one inspectable list, and tests can register a fake without
    monkeypatching.
    """

    def __init__(self) -> None:
        self._sources: dict[str, JobSource] = {}

    def register(self, source: JobSource) -> None:
        self._sources[source.name] = source

    def get(self, name: str) -> JobSource | None:
        return self._sources.get(name)

    def all(self) -> list[JobSource]:
        return list(self._sources.values())

    def names(self) -> list[str]:
        return sorted(self._sources)

    def __len__(self) -> int:
        return len(self._sources)
