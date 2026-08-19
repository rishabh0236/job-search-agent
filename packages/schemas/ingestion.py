"""Extraction contracts.

An ``ExtractedDocument`` is the deterministic, verifiable layer between a file on
disk and anything a model is allowed to say about it. Every block carries a
locator and verbatim text, so a claim can always be traced to a specific place in
the candidate's own material — and so a quote can be checked rather than trusted.
"""

from __future__ import annotations

from pydantic import Field

from packages.schemas.common import Schema
from packages.schemas.enums import SourceType


class BlockKind(Schema):
    """Not an enum: block kinds are advisory hints, extended per source type."""

    name: str


class ExtractedBlock(Schema):
    """One addressable unit of source text (a line, bullet or heading)."""

    locator: str = Field(description="Stable position, e.g. 'page=2;line=14' or 'line=42'")
    text: str = Field(min_length=1, description="Verbatim source text, never paraphrased")
    kind: str = Field(default="line", description="line | bullet | heading | preamble")
    #: Canonical section this block sits under ("experience", "skills", ...).
    section: str | None = None
    #: Character offsets into the raw source. Populated for LaTeX so the M3
    #: patcher can address the exact span; None for PDF, where offsets are not
    #: meaningful for editing.
    start_offset: int | None = Field(default=None, ge=0)
    end_offset: int | None = Field(default=None, ge=0)


class ExtractedDocument(Schema):
    """Result of extracting one resume file."""

    source_type: SourceType
    source_path: str
    sha256: str = Field(min_length=64, max_length=64)
    raw_text: str = ""
    blocks: list[ExtractedBlock] = Field(default_factory=list)
    page_count: int | None = None
    #: Ordered canonical section names detected in the document.
    sections: list[str] = Field(default_factory=list)
    #: LaTeX only: everything before \begin{document}, hashed so a patch that
    #: disturbs the template can be detected (M3).
    preamble_sha256: str | None = None
    warnings: list[str] = Field(default_factory=list)

    def blocks_in(self, section: str) -> list[ExtractedBlock]:
        return [block for block in self.blocks if block.section == section]

    def text_of(self, locator: str) -> str | None:
        for block in self.blocks:
            if block.locator == locator:
                return block.text
        return None


class IngestionFinding(Schema):
    """A problem detected while turning a document into facts."""

    severity: str = Field(description="error | warning | info")
    code: str
    message: str
    locator: str | None = None
    claim: str | None = None


class IngestionReport(Schema):
    """What the profile-review screen shows after an upload."""

    resume_id: str
    candidate_id: str
    source_type: SourceType
    sha256: str
    is_original: bool
    block_count: int = 0
    evidence_count: int = 0
    facts_created: int = 0
    facts_needing_review: int = 0
    facts_rejected: int = 0
    sections: list[str] = Field(default_factory=list)
    findings: list[IngestionFinding] = Field(default_factory=list)
    #: False when the model was unavailable and only deterministic extraction ran.
    llm_extraction_ran: bool = False

    @property
    def has_errors(self) -> bool:
        return any(finding.severity == "error" for finding in self.findings)
