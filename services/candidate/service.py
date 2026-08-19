"""Candidate intelligence orchestration.

Pipeline (skills/01): extract -> parse -> normalize -> evidence-link -> validate ->
user review.

Two properties worth noting:

* **The uploaded file is immutable.** It is copied into a content-addressed store
  under ``data/resumes/`` and never written to again. Tailoring (M3) produces new
  versions that record their parent.
* **The model is optional.** Deterministic extraction runs first and always. If no
  provider is configured, ingestion still yields contact details, links and skills,
  and the report says plainly that model extraction did not run.
"""

from __future__ import annotations

import shutil
from pathlib import Path

from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from packages.core import audit
from packages.core.db.models import Candidate, CandidateFact, Evidence, FactEvidence, Resume
from packages.core.errors import (
    ConflictError,
    DomainError,
    LLMError,
    NotFoundError,
    ValidationFailed,
)
from packages.core.ids import new_id
from packages.core.llm.base import LLMRequest
from packages.core.llm.client import LLMClient
from packages.core.logging import get_logger
from packages.core.settings import Settings, get_settings
from packages.prompts.candidate import (
    RESUME_EXTRACTOR_SYSTEM,
    RESUME_EXTRACTOR_VERSION,
    format_evidence_blocks,
    resume_extractor_user_message,
)
from packages.schemas.candidate import (
    CandidateFact as CandidateFactSchema,
)
from packages.schemas.candidate import (
    CandidatePreferences,
    CandidateProfile,
)
from packages.schemas.enums import FactCategory, Provenance, SourceType
from packages.schemas.ingestion import ExtractedDocument, IngestionFinding, IngestionReport
from packages.schemas.llm_tasks import (
    TASK_RESUME_EXTRACTOR,
    ProposedFact,
    ResumeExtractionOutput,
)
from services.candidate import evidence as evidence_service
from services.candidate import extraction
from services.candidate import facts as facts_service
from services.candidate.evidence import evidence_id_for
from services.candidate.parsing import extract_deterministic_facts

logger = get_logger(__name__)

#: Blocks sent to the extractor. A resume that exceeds this is truncated with a
#: warning rather than silently dropped — better a partial profile the user can see
#: is partial than a request that fails or costs unboundedly.
MAX_EVIDENCE_BLOCKS = 400


class CandidateService:
    """Business logic for candidates, resumes and canonical facts."""

    def __init__(
        self,
        session: Session,
        llm: LLMClient | None = None,
        settings: Settings | None = None,
    ) -> None:
        self._session = session
        self._llm = llm
        self._settings = settings or get_settings()

    # ------------------------------------------------------------- candidates

    def create_candidate(
        self,
        *,
        display_name: str | None = None,
        preferences: CandidatePreferences | None = None,
    ) -> Candidate:
        candidate = Candidate(
            id=new_id("candidate"),
            display_name=display_name,
            preferences=(preferences or CandidatePreferences()).model_dump(mode="json"),
        )
        self._session.add(candidate)
        audit.record(
            self._session,
            actor=audit.ACTOR_USER,
            action="candidate.created",
            entity_type="candidate",
            entity_id=candidate.id,
        )
        self._session.flush()
        return candidate

    def get_candidate(self, candidate_id: str) -> Candidate:
        candidate = self._session.get(Candidate, candidate_id)
        if candidate is None:
            raise NotFoundError(f"candidate {candidate_id} not found")
        return candidate

    def update_preferences(self, candidate_id: str, preferences: CandidatePreferences) -> Candidate:
        """Replace target-role preferences.

        Preferences are what the candidate wants; they are never evidence and never
        become facts (FR-06).
        """
        candidate = self.get_candidate(candidate_id)
        candidate.preferences = preferences.model_dump(mode="json")
        audit.record(
            self._session,
            actor=audit.ACTOR_USER,
            action="candidate.preferences_updated",
            entity_type="candidate",
            entity_id=candidate_id,
            metadata={"target_roles": [role.title for role in preferences.target_roles]},
        )
        self._session.flush()
        return candidate

    def get_profile(self, candidate_id: str) -> CandidateProfile:
        candidate = self.get_candidate(candidate_id)

        rows = self._session.scalars(
            select(CandidateFact)
            .where(CandidateFact.candidate_id == candidate_id)
            .order_by(CandidateFact.category, CandidateFact.claim)
        ).all()

        fact_schemas = [
            CandidateFactSchema(
                id=row.id,
                candidate_id=row.candidate_id,
                category=row.category,
                claim=row.claim,
                attributes=dict(row.attributes),
                evidence=[evidence_service.to_ref(record) for record in row.evidence],
                confidence=row.confidence,
                provenance=row.provenance,
                verified=row.verified,
            )
            for row in rows
        ]

        return CandidateProfile(
            id=candidate.id,
            display_name=candidate.display_name,
            preferences=CandidatePreferences.model_validate(candidate.preferences or {}),
            facts=fact_schemas,
        )

    # ------------------------------------------------------------ fact review

    def _get_fact(self, candidate_id: str, fact_id: str) -> CandidateFact:
        fact = self._session.get(CandidateFact, fact_id)
        if fact is None or fact.candidate_id != candidate_id:
            raise NotFoundError(f"fact {fact_id} not found for candidate {candidate_id}")
        return fact

    def verify_fact(self, candidate_id: str, fact_id: str) -> CandidateFact:
        """Mark a fact as confirmed by the user (FR-05).

        A fact with no evidence cannot be verified as a resume fact; confirming it
        makes it user-provided, which is an honest description of where it came from.
        """
        fact = self._get_fact(candidate_id, fact_id)
        if not fact.evidence:
            fact.provenance = Provenance.USER
        fact.verified = True
        fact.confidence = max(fact.confidence, 1.0 if fact.evidence else 0.8)
        audit.record(
            self._session,
            actor=audit.ACTOR_USER,
            action="candidate.fact_verified",
            entity_type="candidate_fact",
            entity_id=fact_id,
            metadata={"category": fact.category.value, "had_evidence": bool(fact.evidence)},
        )
        self._session.flush()
        return fact

    def correct_fact(self, candidate_id: str, fact_id: str, claim: str) -> CandidateFact:
        """Apply a user correction.

        The corrected text becomes user-provided rather than resume-extracted: the
        user is now the source, and the UI must not present their edit as something
        the resume said.
        """
        if not claim.strip():
            raise ValidationFailed("corrected claim must not be empty")

        fact = self._get_fact(candidate_id, fact_id)
        previous = fact.claim
        fact.claim = claim.strip()
        fact.provenance = Provenance.USER
        fact.verified = True
        fact.confidence = 1.0
        audit.record(
            self._session,
            actor=audit.ACTOR_USER,
            action="candidate.fact_corrected",
            entity_type="candidate_fact",
            entity_id=fact_id,
            metadata={"category": fact.category.value, "previous_length": len(previous)},
        )
        self._session.flush()
        return fact

    def add_fact(
        self,
        candidate_id: str,
        *,
        category: FactCategory,
        claim: str,
        attributes: dict[str, str] | None = None,
    ) -> CandidateFact:
        """Add a fact the resume does not contain.

        Stored as user-provided and verified: the candidate asserting something is
        its own evidence, and it is labelled as such everywhere it appears.
        """
        if not claim.strip():
            raise ValidationFailed("claim must not be empty")

        self.get_candidate(candidate_id)
        fact_id = facts_service.fact_id_for(candidate_id, category, claim.strip())
        if self._session.get(CandidateFact, fact_id) is not None:
            raise ConflictError("an identical fact already exists in this category")

        fact = CandidateFact(
            id=fact_id,
            candidate_id=candidate_id,
            category=category,
            claim=claim.strip(),
            attributes=dict(attributes or {}),
            confidence=1.0,
            provenance=Provenance.USER,
            verified=True,
        )
        self._session.add(fact)
        audit.record(
            self._session,
            actor=audit.ACTOR_USER,
            action="candidate.fact_added",
            entity_type="candidate_fact",
            entity_id=fact_id,
            metadata={"category": category.value},
        )
        self._session.flush()
        return fact

    def delete_fact(self, candidate_id: str, fact_id: str) -> None:
        fact = self._get_fact(candidate_id, fact_id)
        self._session.execute(delete(FactEvidence).where(FactEvidence.fact_id == fact_id))
        self._session.delete(fact)
        audit.record(
            self._session,
            actor=audit.ACTOR_USER,
            action="candidate.fact_deleted",
            entity_type="candidate_fact",
            entity_id=fact_id,
            metadata={"category": fact.category.value},
        )
        self._session.flush()

    # --------------------------------------------------------------- resumes

    def _store_source_file(self, candidate_id: str, source: Path, digest: str) -> Path:
        """Copy the upload into the immutable, content-addressed resume store."""
        target_dir = self._settings.resumes_dir / candidate_id
        target_dir.mkdir(parents=True, exist_ok=True)
        target = target_dir / f"{digest[:16]}{source.suffix.lower()}"
        if not target.exists():
            shutil.copy2(source, target)
            target.chmod(0o440)  # read-only: the original is never rewritten
        return target

    def ingest_resume(
        self,
        candidate_id: str,
        source_path: Path,
        *,
        source_type: SourceType | None = None,
        is_original: bool | None = None,
    ) -> IngestionReport:
        """Ingest a resume file into evidence and canonical facts."""
        self.get_candidate(candidate_id)

        document = extraction.extract(source_path, source_type)
        stored_path = self._store_source_file(candidate_id, source_path, document.sha256)

        existing = self._session.scalar(
            select(Resume).where(
                Resume.candidate_id == candidate_id, Resume.sha256 == document.sha256
            )
        )
        if existing is not None:
            raise ConflictError(
                "this exact file has already been ingested",
                details={"resume_id": existing.id, "sha256": document.sha256},
            )

        # The first resume is the immutable original unless told otherwise.
        has_original = (
            self._session.scalar(
                select(func.count())
                .select_from(Resume)
                .where(Resume.candidate_id == candidate_id, Resume.is_original.is_(True))
            )
            or 0
        )
        resolved_original = is_original if is_original is not None else has_original == 0
        if resolved_original and has_original:
            raise ConflictError("this candidate already has an immutable original resume")

        next_version = (
            self._session.scalar(
                select(func.coalesce(func.max(Resume.version), 0)).where(
                    Resume.candidate_id == candidate_id
                )
            )
            or 0
        ) + 1

        resume = Resume(
            id=new_id("resume"),
            candidate_id=candidate_id,
            source_type=document.source_type,
            source_path=str(stored_path),
            sha256=document.sha256,
            version=next_version,
            is_original=resolved_original,
            derived_from_id=None,
        )
        self._session.add(resume)
        self._session.flush()

        findings: list[IngestionFinding] = [
            IngestionFinding(severity="warning", code="extraction_warning", message=warning)
            for warning in document.warnings
        ]

        evidence_records = evidence_service.persist_document_evidence(
            self._session,
            candidate_id=candidate_id,
            source_id=resume.id,
            document=document,
        )
        locator_to_evidence = {record.locator: record for record in evidence_records.values()}

        validated: list[facts_service.ValidatedFact] = []
        rejected = 0

        # --- deterministic pass: always runs, needs no model
        for deterministic in extract_deterministic_facts(document):
            fact, fact_findings = facts_service.validate_deterministic(
                deterministic, locator_to_evidence
            )
            findings.extend(fact_findings)
            if fact is None:
                rejected += 1
            else:
                validated.append(fact)

        # --- model pass: interpretive facts
        llm_ran = False
        if self._llm is not None:
            proposals, llm_findings, llm_ran = self._propose_facts(
                document, resume.id, evidence_records
            )
            findings.extend(llm_findings)
            for proposal in proposals:
                fact, fact_findings = facts_service.validate_proposal(proposal, evidence_records)
                findings.extend(fact_findings)
                if fact is None:
                    rejected += 1
                else:
                    validated.append(fact)

        created, updated = facts_service.persist(
            self._session, candidate_id=candidate_id, facts=validated
        )

        needing_review = sum(
            1 for fact in validated if fact.provenance is Provenance.UNKNOWN or not fact.verified
        )

        audit.record(
            self._session,
            actor=audit.ACTOR_SYSTEM,
            action="resume.ingested",
            entity_type="resume",
            entity_id=resume.id,
            metadata={
                "source_type": document.source_type.value,
                "sha256": document.sha256,
                "is_original": resolved_original,
                "blocks": len(document.blocks),
                "evidence": len(evidence_records),
                "facts_created": created,
                "facts_updated": updated,
                "facts_rejected": rejected,
                "llm_extraction_ran": llm_ran,
                "prompt_version": RESUME_EXTRACTOR_VERSION,
            },
        )

        logger.info(
            "resume.ingested",
            extra={
                "resume_id": resume.id,
                "blocks": len(document.blocks),
                "facts_created": created,
                "facts_rejected": rejected,
                "llm_extraction_ran": llm_ran,
            },
        )

        return IngestionReport(
            resume_id=resume.id,
            candidate_id=candidate_id,
            source_type=document.source_type,
            sha256=document.sha256,
            is_original=resolved_original,
            block_count=len(document.blocks),
            evidence_count=len(evidence_records),
            facts_created=created,
            facts_needing_review=needing_review,
            facts_rejected=rejected,
            sections=document.sections,
            findings=findings,
            llm_extraction_ran=llm_ran,
        )

    def _propose_facts(
        self,
        document: ExtractedDocument,
        source_id: str,
        evidence_records: dict[str, Evidence],
    ) -> tuple[list[ProposedFact], list[IngestionFinding], bool]:
        """Ask the extractor for interpretive facts.

        A model failure degrades the result, it does not fail the ingestion: the
        deterministic facts are already valid and the user can retry extraction.
        """
        if self._llm is None:  # pragma: no cover - guarded by the caller
            return [], [], False

        # Evidence ids are content-addressed, so the id for a block is derivable
        # without looking anything up.
        listing_pairs = [
            (evidence_id_for(source_id, block.locator, block.text), block)
            for block in document.blocks
        ]
        findings: list[IngestionFinding] = []

        if len(listing_pairs) > MAX_EVIDENCE_BLOCKS:
            findings.append(
                IngestionFinding(
                    severity="warning",
                    code="evidence_truncated",
                    message=(
                        f"resume has {len(listing_pairs)} blocks; only the first "
                        f"{MAX_EVIDENCE_BLOCKS} were sent for extraction"
                    ),
                )
            )
            listing_pairs = listing_pairs[:MAX_EVIDENCE_BLOCKS]

        request: LLMRequest[ResumeExtractionOutput] = LLMRequest(
            task=TASK_RESUME_EXTRACTOR,
            system=RESUME_EXTRACTOR_SYSTEM,
            blocks=[resume_extractor_user_message(format_evidence_blocks(listing_pairs))],
            output_model=ResumeExtractionOutput,
            # Pinned: extraction must be reproducible for golden tests.
            temperature=0.0,
            max_tokens=16000,
            allowed_evidence_ids=frozenset(evidence_records),
        )

        try:
            result = self._llm.run(request)
        except DomainError as exc:
            severity = "warning" if isinstance(exc, LLMError) else "error"
            findings.append(
                IngestionFinding(
                    severity=severity,
                    code="llm_extraction_failed",
                    message=(
                        f"model extraction did not run ({exc.code}): {exc.message}. "
                        "Deterministic extraction still applied."
                    ),
                )
            )
            logger.warning(
                "resume.llm_extraction_failed", extra={"code": exc.code, "detail": exc.message}
            )
            return [], findings, False

        for note in result.output.uncertain:
            findings.append(
                IngestionFinding(
                    severity="info",
                    code="model_uncertain",
                    message=f"model could not attribute an observation to evidence: {note}",
                )
            )

        return list(result.output.facts), findings, True
