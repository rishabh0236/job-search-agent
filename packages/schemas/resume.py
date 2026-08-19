"""Resume records and the structured LaTeX edit contract.

The original is immutable: every tailored version is a new record with its own
sha256. Edits are proposed as discrete operations against uniquely-addressable
targets, verified against exact ``old_text``, then applied by deterministic code.
"""

from __future__ import annotations

from datetime import datetime

from pydantic import Field, model_validator

from packages.schemas.common import Confidence, EvidenceRef, Schema
from packages.schemas.enums import EditOperation, EditStatus, SourceType, TailoringMode


class Resume(Schema):
    """A stored resume artifact (original or tailored)."""

    id: str
    candidate_id: str
    source_type: SourceType
    source_path: str
    sha256: str = Field(min_length=64, max_length=64)
    version: int = Field(ge=1)
    # None for the immutable original; set for every derived version.
    derived_from_id: str | None = None
    job_id: str | None = Field(default=None, description="Set when tailored for a job")
    is_original: bool = False
    created_at: datetime

    @model_validator(mode="after")
    def _original_has_no_parent(self) -> Resume:
        if self.is_original and self.derived_from_id is not None:
            raise ValueError("the original resume cannot be derived from another version")
        if not self.is_original and self.derived_from_id is None:
            raise ValueError("a tailored resume must record derived_from_id")
        return self


class ResumeSection(Schema):
    """A detected region of the LaTeX source, addressable by a stable id."""

    target_id: str = Field(description="Stable handle, e.g. 'experience.acme.bullet.2'")
    kind: str = Field(description="section | subsection | bullet | summary | skills")
    title: str | None = None
    text: str
    start_offset: int = Field(ge=0)
    end_offset: int = Field(ge=0)

    @model_validator(mode="after")
    def _offsets_ordered(self) -> ResumeSection:
        if self.end_offset < self.start_offset:
            raise ValueError("end_offset must not precede start_offset")
        return self


class ResumeAst(Schema):
    """Parsed, safe intermediate representation of a .tex file (FR-31)."""

    resume_id: str
    preamble_sha256: str = Field(
        min_length=64,
        max_length=64,
        description="Guards template/macros: a changed preamble means an unsafe patch",
    )
    sections: list[ResumeSection] = Field(default_factory=list)

    def target(self, target_id: str) -> ResumeSection | None:
        matches = [s for s in self.sections if s.target_id == target_id]
        if len(matches) != 1:
            return None
        return matches[0]


class ResumeEdit(Schema):
    """One proposed, evidence-linked edit operation (FR-32/FR-33)."""

    id: str
    resume_id: str
    job_id: str | None = None
    operation: EditOperation
    target_id: str
    old_text: str = Field(description="Exact current text; verified before patching")
    new_text: str = Field(default="", description="Replacement text; empty for deletions")
    # Non-empty for any edit that changes factual content. Pure reordering and
    # keyword-only rephrasing may legitimately have none, which is why this is
    # enforced by the tailoring service against the operation, not the schema.
    evidence_refs: list[EvidenceRef] = Field(default_factory=list)
    rationale: str = ""
    confidence: Confidence = 0.0
    status: EditStatus = EditStatus.PROPOSED

    @property
    def changes_content(self) -> bool:
        """Reordering preserves wording; everything else can alter meaning."""
        return self.operation is not EditOperation.REORDER_ITEMS


class TailoringRequest(Schema):
    candidate_id: str
    resume_id: str
    job_id: str
    mode: TailoringMode = TailoringMode.BALANCED
    max_edits: int = Field(default=20, ge=1, le=100)


class CompileResult(Schema):
    """Outcome of a LaTeX compilation (FR-35/FR-36)."""

    success: bool
    pdf_path: str | None = None
    log_excerpt: str = ""
    page_count: int | None = None
    duration_ms: int = 0
    engine: str = ""


class ValidationFinding(Schema):
    """A problem detected after patching. Any error blocks the version."""

    severity: str = Field(description="error | warning")
    code: str
    message: str
    target_id: str | None = None


class TailoringResult(Schema):
    """Everything the review screen needs to show a diff and its warnings."""

    resume_id: str
    job_id: str
    mode: TailoringMode
    edits: list[ResumeEdit] = Field(default_factory=list)
    compile_result: CompileResult | None = None
    findings: list[ValidationFinding] = Field(default_factory=list)

    @property
    def blocked(self) -> bool:
        """True when this version must not be offered for submission."""
        if self.compile_result is not None and not self.compile_result.success:
            return True
        return any(finding.severity == "error" for finding in self.findings)
