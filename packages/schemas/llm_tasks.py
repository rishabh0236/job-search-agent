"""Output schemas for LLM tasks.

Separate from the domain schemas on purpose. A model proposes *these*; deterministic
code validates them and only then constructs the domain object. The two must never
be the same class, or an unvalidated proposal could be persisted by accident.

Task names are the registry keys used by ``LLMRequest.task`` and by the stub
provider's fixtures.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from packages.schemas.enums import FactCategory, RequirementKind

TASK_RESUME_EXTRACTOR = "resume_extractor"
TASK_CANDIDATE_NORMALIZER = "candidate_normalizer"
TASK_JOB_ANALYZER = "job_analyzer"
TASK_MATCH_EXPLAINER = "match_explainer"
TASK_RESUME_EDITOR = "resume_editor"
TASK_COVER_LETTER = "cover_letter_writer"
TASK_ANSWER_MAPPER = "application_question_mapper"


class LLMSchema(BaseModel):
    """Base for model-facing schemas.

    ``extra="forbid"`` matters here: a model that invents an extra field is
    telling us it is operating outside the contract, and we want that to fail
    loudly rather than be silently dropped.
    """

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class ProposedFact(LLMSchema):
    """One fact the model believes the resume supports."""

    category: FactCategory = Field(description="Which fact category this belongs to")
    claim: str = Field(
        min_length=1,
        max_length=1000,
        description=(
            "The normalized assertion, restated from the source. Must not add "
            "information that is not present in the cited evidence."
        ),
    )
    evidence_ids: list[str] = Field(
        default_factory=list,
        description=(
            "Ids of supplied evidence blocks that support this claim, copied "
            "verbatim from the input. Never invent an id."
        ),
    )
    attributes: dict[str, str] = Field(
        default_factory=dict,
        description=(
            "Optional structured detail present in the source: employer, title, "
            "start_date, end_date, institution, degree, issuer, level. Omit any "
            "field the source does not state."
        ),
    )
    confidence: float = Field(
        ge=0.0,
        le=1.0,
        description="How clearly the cited evidence supports the claim",
    )


class ResumeExtractionOutput(LLMSchema):
    """Full proposal set from one resume."""

    facts: list[ProposedFact] = Field(default_factory=list, max_length=400)
    #: Things the model saw but could not attribute to evidence. These become
    #: UNKNOWN items for the user to confirm, never facts.
    uncertain: list[str] = Field(
        default_factory=list,
        max_length=50,
        description="Observations that lack clear supporting text",
    )


class NormalizedSkill(LLMSchema):
    """A skill mapped to a canonical name."""

    raw: str = Field(min_length=1)
    canonical: str = Field(min_length=1)
    evidence_ids: list[str] = Field(default_factory=list)


class CandidateNormalizationOutput(LLMSchema):
    skills: list[NormalizedSkill] = Field(default_factory=list, max_length=300)


class MatchExplanationOutput(LLMSchema):
    """Prose explanation of an already-computed match."""

    explanation: str = Field(
        min_length=1,
        max_length=2000,
        description=(
            "Three to five sentences explaining the match, citing supplied evidence "
            "ids in square brackets. Never introduces a strength that was not listed."
        ),
    )
    cited_evidence_ids: list[str] = Field(
        default_factory=list,
        description="Evidence ids referenced in the explanation, copied verbatim",
    )


class AnalyzedRequirement(LLMSchema):
    """One requirement the analyzer read out of a posting."""

    text: str = Field(min_length=1, max_length=400)
    kind: RequirementKind = Field(
        description="required if the posting demands it, preferred if optional, else contextual"
    )
    key: str | None = Field(
        default=None,
        description="Short normalized handle such as 'python' or 'years_experience>=5'",
    )


class JobAnalysisOutput(LLMSchema):
    """Requirements extracted from a job description."""

    requirements: list[AnalyzedRequirement] = Field(default_factory=list, max_length=80)
    #: Anything in the posting that looked like an instruction to the reader/system.
    #: Surfaced so prompt-injection attempts are visible rather than merely ignored.
    suspicious_instructions: list[str] = Field(default_factory=list, max_length=20)


class ProposedEdit(LLMSchema):
    """One edit operation against a resume target."""

    target_id: str = Field(min_length=1, description="Exact target id from the supplied listing")
    old_text: str = Field(min_length=1, description="The target's current text, copied verbatim")
    new_text: str = Field(
        min_length=1, max_length=1000, description="Replacement prose. No LaTeX structure."
    )
    evidence_ids: list[str] = Field(
        default_factory=list,
        description="Ids of the evidence supporting every factual element of new_text",
    )
    rationale: str = Field(
        default="", max_length=400, description="Why this wording suits the posting"
    )
    confidence: float = Field(ge=0.0, le=1.0)


class ResumeEditingOutput(LLMSchema):
    edits: list[ProposedEdit] = Field(default_factory=list, max_length=100)
    #: Requirements the resume genuinely does not evidence. Surfaced as gaps for the
    #: user, never quietly filled in with an invented claim.
    unaddressed_requirements: list[str] = Field(default_factory=list, max_length=40)


class ProposedAnswer(LLMSchema):
    """A model-proposed answer to a form question."""

    field_name: str = Field(min_length=1)
    answer: str = Field(default="", max_length=4000)
    evidence_ids: list[str] = Field(default_factory=list)
    confidence: float = Field(ge=0.0, le=1.0)
    #: True when the model believes it cannot answer from the supplied facts. The
    #: honest option must always be available, or the model will invent one.
    needs_user: bool = Field(
        default=False, description="Set when the candidate's facts do not answer this"
    )
    reason: str = Field(default="", max_length=300)


class AnswerMappingOutput(LLMSchema):
    answers: list[ProposedAnswer] = Field(default_factory=list, max_length=60)


class CoverLetterOutput(LLMSchema):
    """A drafted cover letter, cited."""

    body: str = Field(min_length=1, max_length=6000)
    cited_evidence_ids: list[str] = Field(default_factory=list)
    #: Claims the model wanted to make but could not support. Surfaced, not silently
    #: dropped, so the user can add the fact if it is true.
    omitted_claims: list[str] = Field(default_factory=list, max_length=20)
