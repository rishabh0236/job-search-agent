"""Prefixed identifiers.

Readable IDs make logs, audit trails and support conversations far easier than
bare UUIDs: ``job_9f2c...`` tells you what you are looking at. They are opaque
strings in the database, so switching generation strategy later is safe.
"""

from __future__ import annotations

from uuid import uuid4

# Keep in sync with the entities in packages/schemas.
PREFIXES: dict[str, str] = {
    "candidate": "cand",
    "candidate_fact": "fact",
    "evidence": "ev",
    "resume": "res",
    "resume_edit": "edit",
    "job": "job",
    "job_match": "match",
    "application": "app",
    "application_answer": "ans",
    "artifact": "art",
    "audit_event": "aud",
}


def new_id(kind: str) -> str:
    """Return a fresh identifier for ``kind`` (a key of :data:`PREFIXES`)."""
    try:
        prefix = PREFIXES[kind]
    except KeyError as exc:  # pragma: no cover - programming error
        raise KeyError(f"unknown id kind {kind!r}; add it to PREFIXES") from exc
    return f"{prefix}_{uuid4().hex}"
