"""Primitives shared across the domain: evidence references and claim wrappers."""

from __future__ import annotations

from datetime import datetime
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field

from packages.schemas.enums import Provenance

#: A confidence score. Deterministic code sets this; the LLM may propose it but
#: never gets to decide whether something is "verified".
Confidence = Annotated[float, Field(ge=0.0, le=1.0)]


class Schema(BaseModel):
    """Base for all domain schemas: strict, immutable-by-convention DTOs."""

    model_config = ConfigDict(
        extra="forbid",
        str_strip_whitespace=True,
        validate_assignment=True,
    )


class EvidenceRef(Schema):
    """A pointer into source material that supports a claim.

    Every non-UNKNOWN candidate fact carries at least one of these. Semantic
    similarity is explicitly *not* evidence (skills/01) — a reference must locate
    actual text the candidate supplied.
    """

    evidence_id: str = Field(description="Stable id of the evidence record")
    source_id: str = Field(description="Resume/document id this text came from")
    locator: str = Field(
        description="Where in the source: page/line/section, e.g. 'page=2;line=14'"
    )
    quote: str = Field(
        min_length=1,
        max_length=2000,
        description="Verbatim supporting text, copied not paraphrased",
    )

    def label(self) -> str:
        return f"{self.source_id}@{self.locator}"


class Claim(Schema):
    """A single assertion about the candidate, with its provenance.

    ``provenance`` and ``verified`` together produce the UI trust badge; the two
    are kept separate so an AI suggestion the user later confirms becomes
    user-provided-and-verified rather than silently "true".
    """

    text: str = Field(min_length=1)
    provenance: Provenance = Provenance.UNKNOWN
    confidence: Confidence = 0.0
    verified: bool = False
    evidence: list[EvidenceRef] = Field(default_factory=list)

    @property
    def is_supported(self) -> bool:
        """True when this claim may be used in generated artifacts."""
        if self.provenance is Provenance.UNKNOWN:
            return False
        if self.provenance is Provenance.USER:
            return True  # the user asserted it; that is its own evidence
        return bool(self.evidence)

    def trust_label(self) -> str:
        """Human-facing label matching the four required UI categories."""
        if self.provenance is Provenance.UNKNOWN:
            return "Unknown / requires confirmation"
        if self.provenance is Provenance.USER:
            return "User-provided information"
        if self.provenance is Provenance.AI:
            return "AI suggestion"
        return "Verified candidate fact" if self.verified else "Extracted (unverified)"


class Timestamped(Schema):
    created_at: datetime
    updated_at: datetime
