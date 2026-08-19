"""Resume tailoring orchestration.

The full loop from skills/04 and skills/05:

    original .tex -> AST -> proposed edits -> validate -> patch -> compile
                 -> compare against the original PDF -> new version -> review

Nothing here mutates the original: a tailored resume is always a new ``Resume`` row
recording ``derived_from_id``, with its own file and sha256. If compilation fails or
validation finds an error, no version is created at all — a broken artifact must not
be reachable from the review screen.

The factuality guard runs *twice* on different grounds: the model is told not to
invent, and then every proposed edit is checked against the evidence it cites. The
second check is the one that counts.
"""

from __future__ import annotations

from pathlib import Path

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from packages.core import audit
from packages.core.db.models import CandidateFact as CandidateFactRow
from packages.core.db.models import Evidence
from packages.core.db.models import Resume as ResumeRow
from packages.core.db.models import ResumeEdit as ResumeEditRow
from packages.core.errors import DomainError, NotFoundError, ValidationFailed
from packages.core.ids import new_id
from packages.core.llm.base import LLMRequest, UntrustedContent
from packages.core.llm.client import LLMClient
from packages.core.llm.guards import find_unsupported_metrics
from packages.core.logging import get_logger
from packages.core.settings import Settings, get_settings
from packages.prompts.tailoring import (
    RESUME_EDITOR_SYSTEM,
    RESUME_EDITOR_VERSION,
    resume_editor_user_message,
)
from packages.schemas.enums import EditOperation, EditStatus, SourceType, TailoringMode
from packages.schemas.job import Job
from packages.schemas.llm_tasks import (
    TASK_RESUME_EDITOR,
    ProposedEdit,
    ResumeEditingOutput,
)
from packages.schemas.resume import (
    CompileResult,
    ResumeAst,
    ResumeEdit,
    TailoringResult,
    ValidationFinding,
)
from services.candidate import extraction
from services.resume import ast as ast_module
from services.resume import patcher
from services.resume.compiler import LatexCompiler, build_compiler, compare_output, pdf_text

logger = get_logger(__name__)


class TailoringService:
    def __init__(
        self,
        session: Session,
        llm: LLMClient | None = None,
        settings: Settings | None = None,
        compiler: LatexCompiler | None = None,
    ) -> None:
        self._session = session
        self._llm = llm
        self._settings = settings or get_settings()
        self._compiler = compiler or build_compiler(self._settings)

    # -------------------------------------------------------------------- reads

    def _resume_row(self, resume_id: str) -> ResumeRow:
        row = self._session.get(ResumeRow, resume_id)
        if row is None:
            raise NotFoundError(f"resume {resume_id} not found")
        return row

    def load_source(self, resume_id: str) -> str:
        row = self._resume_row(resume_id)
        if row.source_type is not SourceType.LATEX:
            raise ValidationFailed(
                "only LaTeX resumes can be tailored; import the .tex source",
                details={"source_type": row.source_type.value},
            )
        path = Path(row.source_path)
        if not path.is_file():
            raise NotFoundError(f"resume file missing on disk: {path}")
        return path.read_text(encoding="utf-8")

    def build_ast(self, resume_id: str) -> ResumeAst:
        source = self.load_source(resume_id)
        document = extraction.extract_latex_source(
            source, source_path=self._resume_row(resume_id).source_path, sha256="0" * 64
        )
        return ast_module.build_ast(document, resume_id)

    # ---------------------------------------------------------------- proposals

    def _evidence_for_targets(
        self, candidate_id: str, ast: ResumeAst
    ) -> tuple[str, dict[str, Evidence]]:
        """Build the target listing for the prompt, with each target's evidence.

        A target is paired with the evidence whose quote matches its text, which is
        how the editor knows what it is allowed to say about that bullet.
        """
        rows = self._session.scalars(
            select(Evidence).where(Evidence.candidate_id == candidate_id)
        ).all()
        by_normalised = {patcher._normalise(row.quote): row for row in rows}
        available = {row.id: row for row in rows}

        lines: list[str] = []
        for section in ast_module.editable_targets(ast):
            match = by_normalised.get(patcher._normalise(section.text))
            citation = f" [evidence: {match.id}]" if match else " [evidence: none]"
            lines.append(
                f"- target_id={section.target_id} ({section.kind}){citation}\n  {section.text}"
            )

        return "\n".join(lines), available

    def propose_edits(
        self,
        candidate_id: str,
        resume_id: str,
        job: Job,
        *,
        mode: TailoringMode = TailoringMode.BALANCED,
        max_edits: int = 20,
    ) -> tuple[list[ProposedEdit], list[str], list[ValidationFinding]]:
        """Ask the editor for edits. Returns proposals, unaddressed gaps, findings."""
        if self._llm is None:
            return (
                [],
                [],
                [
                    ValidationFinding(
                        severity="warning",
                        code="llm_unavailable",
                        message="no model configured; no edits were proposed",
                    )
                ],
            )

        ast = self.build_ast(resume_id)
        listing, available = self._evidence_for_targets(candidate_id, ast)

        request: LLMRequest[ResumeEditingOutput] = LLMRequest(
            task=TASK_RESUME_EDITOR,
            system=RESUME_EDITOR_SYSTEM,
            blocks=[
                resume_editor_user_message(
                    mode=mode,
                    job_title=job.title,
                    company=job.company,
                    requirements=[item.text for item in job.requirements],
                    targets_listing=listing,
                    max_edits=max_edits,
                ),
                UntrustedContent(
                    label=f"{job.source}:{job.source_job_id}",
                    text=job.description[:6000],
                ),
            ],
            output_model=ResumeEditingOutput,
            temperature=0.0,
            max_tokens=8000,
            allowed_evidence_ids=frozenset(available),
        )

        try:
            result = self._llm.run(request)
        except DomainError as exc:
            return (
                [],
                [],
                [
                    ValidationFinding(
                        severity="error",
                        code="edit_proposal_failed",
                        message=f"the editor did not return usable edits ({exc.code}): {exc.message}",
                    )
                ],
            )

        return list(result.output.edits), list(result.output.unaddressed_requirements), []

    # --------------------------------------------------------------- validation

    def validate_factuality(
        self,
        proposal: ProposedEdit,
        available_evidence: dict[str, Evidence],
    ) -> ValidationFinding | None:
        """Check a proposed edit against the evidence it cites.

        This is the same reasoning as fact validation in M1, applied to generated
        text: any figure in the new wording must already appear in the candidate's
        own material. It is what stops "improved throughput" becoming "improved
        throughput by 40%".
        """
        cited = [
            available_evidence[key] for key in proposal.evidence_ids if key in available_evidence
        ]
        unknown = [key for key in proposal.evidence_ids if key not in available_evidence]
        if unknown:
            return ValidationFinding(
                severity="error",
                code="fabricated_evidence",
                message=f"edit cites unknown evidence: {', '.join(unknown)}",
                target_id=proposal.target_id,
            )

        # The original text is itself evidence: rephrasing what a bullet already says
        # is the whole point of tailoring.
        supporting = [record.quote for record in cited] + [proposal.old_text]
        unsupported = find_unsupported_metrics(proposal.new_text, supporting)
        if unsupported:
            return ValidationFinding(
                severity="error",
                code="unsupported_metric",
                message=(
                    "edit introduces figures absent from the cited evidence: "
                    + ", ".join(unsupported)
                ),
                target_id=proposal.target_id,
            )

        if not cited and _looks_factual(proposal.new_text, proposal.old_text):
            return ValidationFinding(
                severity="error",
                code="uncited_factual_change",
                message="edit changes factual content but cites no evidence",
                target_id=proposal.target_id,
            )

        return None

    # ------------------------------------------------------------------ tailor

    def tailor(
        self,
        candidate_id: str,
        resume_id: str,
        job: Job,
        *,
        mode: TailoringMode = TailoringMode.BALANCED,
        max_edits: int = 20,
        proposals: list[ProposedEdit] | None = None,
    ) -> TailoringResult:
        """Run the full tailoring loop and create a new version if it is sound."""
        source = self.load_source(resume_id)
        ast = self.build_ast(resume_id)
        _, available = self._evidence_for_targets(candidate_id, ast)

        findings: list[ValidationFinding] = []
        if proposals is None:
            proposals, unaddressed, proposal_findings = self.propose_edits(
                candidate_id, resume_id, job, mode=mode, max_edits=max_edits
            )
            findings.extend(proposal_findings)
            findings.extend(
                ValidationFinding(
                    severity="info",
                    code="unaddressed_requirement",
                    message=f"resume does not evidence: {item}",
                )
                for item in unaddressed
            )

        # Factuality first: a fabricated edit never reaches the patcher.
        edits: list[ResumeEdit] = []
        for proposal in proposals[:max_edits]:
            finding = self.validate_factuality(proposal, available)
            if finding is not None:
                findings.append(finding)
                continue
            edits.append(
                ResumeEdit(
                    id=new_id("resume_edit"),
                    resume_id=resume_id,
                    job_id=job.id,
                    operation=EditOperation.REPLACE_TEXT,
                    target_id=proposal.target_id,
                    old_text=proposal.old_text,
                    new_text=proposal.new_text,
                    evidence_refs=[],
                    rationale=proposal.rationale,
                    confidence=proposal.confidence,
                    status=EditStatus.PROPOSED,
                )
            )

        patch = patcher.apply_edits(source, ast, edits)
        findings.extend(patch.findings)

        compile_result: CompileResult | None = None
        new_resume_id: str | None = None

        if patch.applied and self._compiler.available():
            compile_result, layout_findings = self._compile_and_compare(
                resume_id, source, patch.source
            )
            findings.extend(layout_findings)

            blocked = not compile_result.success or any(f.severity == "error" for f in findings)
            if not blocked:
                new_resume_id = self._store_version(
                    candidate_id, resume_id, job.id, patch.source, patch.applied
                )
        elif patch.applied:
            findings.append(
                ValidationFinding(
                    severity="error",
                    code="compiler_unavailable",
                    message=(
                        "LaTeX engine is not installed, so the tailored resume could not be "
                        "compiled or verified; run scripts/bootstrap.sh"
                    ),
                )
            )

        result = TailoringResult(
            resume_id=new_resume_id or resume_id,
            job_id=job.id,
            mode=mode,
            edits=[edit.model_copy(update={"status": EditStatus.APPLIED}) for edit in patch.applied]
            + [
                edit.model_copy(update={"status": EditStatus.REJECTED})
                for edit, _ in patch.rejected
            ],
            compile_result=compile_result,
            findings=findings,
        )

        audit.record(
            self._session,
            actor=audit.ACTOR_SYSTEM,
            action="resume.tailored",
            entity_type="resume",
            entity_id=new_resume_id or resume_id,
            metadata={
                "source_resume_id": resume_id,
                "job_id": job.id,
                "mode": mode.value,
                "proposed": len(proposals),
                "applied": len(patch.applied),
                "rejected": len(patch.rejected),
                "blocked": result.blocked,
                "new_version_created": new_resume_id is not None,
                "prompt_version": RESUME_EDITOR_VERSION,
            },
        )
        logger.info(
            "resume.tailored",
            extra={
                "source_resume_id": resume_id,
                "applied": len(patch.applied),
                "rejected": len(patch.rejected),
                "blocked": result.blocked,
            },
        )
        return result

    def _compile_and_compare(
        self, original_resume_id: str, original_source: str, tailored_source: str
    ) -> tuple[CompileResult, list[ValidationFinding]]:
        original_result = self._compiler.compile(original_source)
        tailored_result = self._compiler.compile(tailored_source)

        if not tailored_result.success:
            return tailored_result, [
                ValidationFinding(
                    severity="error",
                    code="compilation_failed",
                    message=f"tailored resume did not compile: {tailored_result.log_excerpt[:400]}",
                )
            ]

        findings = compare_output(
            original_result,
            tailored_result,
            original_text=pdf_text(Path(original_result.pdf_path))
            if original_result.pdf_path
            else "",
            tailored_text=pdf_text(Path(tailored_result.pdf_path))
            if tailored_result.pdf_path
            else "",
        )
        return tailored_result, findings

    def _store_version(
        self,
        candidate_id: str,
        source_resume_id: str,
        job_id: str,
        source: str,
        edits: list[ResumeEdit],
    ) -> str:
        """Write a new immutable version and record its edits."""
        digest = extraction.sha256_bytes(source.encode("utf-8"))
        directory = self._settings.resumes_dir / candidate_id
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / f"{digest[:16]}.tex"
        if not path.exists():
            path.write_text(source, encoding="utf-8")
            path.chmod(0o440)

        next_version = (
            self._session.scalar(
                select(func.coalesce(func.max(ResumeRow.version), 0)).where(
                    ResumeRow.candidate_id == candidate_id
                )
            )
            or 0
        ) + 1

        row = ResumeRow(
            id=new_id("resume"),
            candidate_id=candidate_id,
            source_type=SourceType.LATEX,
            source_path=str(path),
            sha256=digest,
            version=next_version,
            derived_from_id=source_resume_id,
            job_id=job_id,
            is_original=False,
        )
        self._session.add(row)
        self._session.flush()

        for edit in edits:
            self._session.add(
                ResumeEditRow(
                    id=edit.id,
                    resume_id=row.id,
                    job_id=job_id,
                    operation=edit.operation,
                    target_id=edit.target_id,
                    old_text=edit.old_text,
                    new_text=edit.new_text,
                    evidence_refs=[ref.evidence_id for ref in edit.evidence_refs],
                    rationale=edit.rationale,
                    confidence=edit.confidence,
                    status=EditStatus.APPLIED,
                )
            )
        self._session.flush()
        return row.id

    # ------------------------------------------------------------------- diffs

    def diff(self, resume_id: str) -> list[dict[str, str]]:
        """Per-target before/after for the review screen (FR-37)."""
        rows = self._session.scalars(
            select(ResumeEditRow).where(ResumeEditRow.resume_id == resume_id)
        ).all()
        return [
            {
                "target_id": row.target_id,
                "before": row.old_text,
                "after": row.new_text,
                "rationale": row.rationale,
                "status": row.status.value,
            }
            for row in rows
        ]

    def facts_for(self, candidate_id: str) -> list[CandidateFactRow]:
        return list(
            self._session.scalars(
                select(CandidateFactRow).where(CandidateFactRow.candidate_id == candidate_id)
            ).all()
        )


def _looks_factual(new_text: str, old_text: str) -> bool:
    """Heuristic: does the rewrite add substantive content rather than rephrase?

    Used only to demand a citation, never to reject on its own. Adding many new
    content words to a bullet is the shape of an invented claim.
    """
    import re

    def words(text: str) -> set[str]:
        return {word for word in re.findall(r"[a-z]{4,}", text.lower())}

    added = words(new_text) - words(old_text)
    return len(added) > 6
