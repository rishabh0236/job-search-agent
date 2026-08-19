"""Evidence creation and quote verification.

Evidence ids are **deterministic**: the same document re-ingested produces the same
ids. That keeps golden tests stable, makes re-ingestion idempotent, and means an
evidence reference stored on a resume edit stays valid across a re-import.

Quote verification is the mechanism behind the product's central claim. A model may
only cite ids it was handed, and this module additionally checks that the text it
attributes to a citation actually appears in the candidate's source.
"""

from __future__ import annotations

import hashlib
import re

from sqlalchemy import select
from sqlalchemy.orm import Session

from packages.core.db.models import Evidence
from packages.schemas.common import EvidenceRef
from packages.schemas.ingestion import ExtractedDocument

_WHITESPACE = re.compile(r"\s+")
_PUNCTUATION = re.compile(r"[^\w\s]")


def evidence_id_for(source_id: str, locator: str, quote: str) -> str:
    """Content-addressed evidence id, stable across ingestions."""
    digest = hashlib.sha256(f"{source_id}|{locator}|{quote}".encode()).hexdigest()
    return f"ev_{digest[:32]}"


def normalize_for_comparison(text: str) -> str:
    """Lowercase, strip punctuation, collapse whitespace.

    Comparison has to survive the difference between LaTeX source, extracted PDF
    text and a model's restatement of the same sentence — while still refusing text
    that is not in the source at all.
    """
    lowered = _PUNCTUATION.sub(" ", text.lower())
    return _WHITESPACE.sub(" ", lowered).strip()


def quote_is_supported(quote: str, source_texts: list[str]) -> bool:
    """True when ``quote`` appears in any of ``source_texts``.

    Substring containment after normalization: exact matching would fail on
    harmless whitespace differences, while token-overlap scoring would let an
    invented sentence through. Containment is the strictest rule that still works
    across three text representations.
    """
    needle = normalize_for_comparison(quote)
    if not needle:
        return False
    return any(needle in normalize_for_comparison(text) for text in source_texts)


def persist_document_evidence(
    session: Session,
    *,
    candidate_id: str,
    source_id: str,
    document: ExtractedDocument,
) -> dict[str, Evidence]:
    """Create one evidence row per extracted block, skipping any that already exist.

    Returns every evidence record for this document keyed by id, whether created
    now or already present, so callers always get the full citable set.
    """
    existing = {
        row.id: row
        for row in session.scalars(
            select(Evidence).where(
                Evidence.candidate_id == candidate_id,
                Evidence.source_id == source_id,
            )
        )
    }

    records: dict[str, Evidence] = dict(existing)
    for block in document.blocks:
        evidence_id = evidence_id_for(source_id, block.locator, block.text)
        if evidence_id in records:
            continue
        record = Evidence(
            id=evidence_id,
            candidate_id=candidate_id,
            source_id=source_id,
            locator=block.locator,
            quote=block.text,
        )
        session.add(record)
        records[evidence_id] = record

    session.flush()
    return records


def to_ref(record: Evidence) -> EvidenceRef:
    return EvidenceRef(
        evidence_id=record.id,
        source_id=record.source_id,
        locator=record.locator,
        quote=record.quote,
    )


def load_refs(session: Session, evidence_ids: list[str]) -> list[EvidenceRef]:
    """Resolve stored evidence into API-facing references, preserving order."""
    if not evidence_ids:
        return []
    rows = {
        row.id: row
        for row in session.scalars(select(Evidence).where(Evidence.id.in_(evidence_ids)))
    }
    return [to_ref(rows[key]) for key in evidence_ids if key in rows]
