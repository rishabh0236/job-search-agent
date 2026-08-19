"""Domain enumerations, shared by Pydantic schemas and the ORM.

Stored as strings in the database so values stay readable in SQLite and portable
to PostgreSQL without native enum migrations.
"""

from __future__ import annotations

from enum import StrEnum


class FactCategory(StrEnum):
    """Categories from skills/01-candidate-intelligence.md."""

    IDENTITY = "identity"
    CONTACT = "contact"
    SUMMARY = "summary"
    EXPERIENCE = "experience"
    ACHIEVEMENT = "achievement"
    SKILL = "skill"
    PROJECT = "project"
    EDUCATION = "education"
    PUBLICATION = "publication"
    CERTIFICATION = "certification"
    LANGUAGE = "language"
    PREFERENCE = "preference"
    WORK_AUTHORIZATION = "work_authorization"
    COMPENSATION = "compensation"
    AVAILABILITY = "availability"


class Provenance(StrEnum):
    """Where a value came from. Drives the four UI trust labels.

    RESUME  -> "Verified candidate fact" (once ``verified`` is set)
    USER    -> "User-provided information"
    AI      -> "AI suggestion" (never persisted as truth without review)
    UNKNOWN -> "Unknown / requires confirmation"
    """

    RESUME = "resume"
    USER = "user"
    AI = "ai_suggestion"
    UNKNOWN = "unknown"


class SourceType(StrEnum):
    """Kind of material a resume record was ingested from."""

    PDF = "pdf"
    LATEX = "latex"
    TEXT = "text"
    DOCX = "docx"


class EmploymentType(StrEnum):
    FULL_TIME = "full_time"
    PART_TIME = "part_time"
    CONTRACT = "contract"
    INTERNSHIP = "internship"
    TEMPORARY = "temporary"
    UNKNOWN = "unknown"


class RemoteMode(StrEnum):
    ONSITE = "onsite"
    HYBRID = "hybrid"
    REMOTE = "remote"
    UNKNOWN = "unknown"


class RequirementKind(StrEnum):
    """Hard constraints are eligibility gates; the rest feed semantic scoring."""

    REQUIRED = "required"
    PREFERRED = "preferred"
    CONTEXTUAL = "contextual"


class Eligibility(StrEnum):
    ELIGIBLE = "eligible"
    INELIGIBLE = "ineligible"
    # A missing fact must never silently become a negative (skills/03).
    UNKNOWN = "unknown"


class TailoringMode(StrEnum):
    CONSERVATIVE = "conservative"
    BALANCED = "balanced"
    AGGRESSIVE = "aggressive"


class EditOperation(StrEnum):
    """Structured LaTeX edits. No unconstrained whole-file rewriting."""

    REPLACE_TEXT = "replace_text"
    INSERT_AFTER = "insert_after"
    DELETE_BLOCK = "delete_block"
    REORDER_ITEMS = "reorder_items"


class EditStatus(StrEnum):
    PROPOSED = "proposed"
    ACCEPTED = "accepted"
    REJECTED = "rejected"
    APPLIED = "applied"


class ApplicationStatus(StrEnum):
    """State machine from skills/06-application-agent.md.

    Happy path:
        CREATED -> PREPARING -> READY_FOR_REVIEW -> USER_APPROVED
                -> SUBMITTING -> SUBMITTED
    Alternates: VERIFICATION_REQUIRED, FAILED, STOPPED.
    """

    CREATED = "created"
    PREPARING = "preparing"
    READY_FOR_REVIEW = "ready_for_review"
    USER_APPROVED = "user_approved"
    SUBMITTING = "submitting"
    SUBMITTED = "submitted"
    VERIFICATION_REQUIRED = "verification_required"
    FAILED = "failed"
    STOPPED = "stopped"


#: Allowed transitions. Deterministic code owns this table, never the LLM.
APPLICATION_TRANSITIONS: dict[ApplicationStatus, frozenset[ApplicationStatus]] = {
    ApplicationStatus.CREATED: frozenset({ApplicationStatus.PREPARING, ApplicationStatus.STOPPED}),
    ApplicationStatus.PREPARING: frozenset(
        {
            ApplicationStatus.READY_FOR_REVIEW,
            ApplicationStatus.FAILED,
            ApplicationStatus.STOPPED,
        }
    ),
    ApplicationStatus.READY_FOR_REVIEW: frozenset(
        {
            ApplicationStatus.USER_APPROVED,
            ApplicationStatus.PREPARING,
            ApplicationStatus.STOPPED,
        }
    ),
    ApplicationStatus.USER_APPROVED: frozenset(
        {
            ApplicationStatus.SUBMITTING,
            ApplicationStatus.STOPPED,
        }
    ),
    ApplicationStatus.SUBMITTING: frozenset(
        {
            ApplicationStatus.SUBMITTED,
            ApplicationStatus.VERIFICATION_REQUIRED,
            ApplicationStatus.FAILED,
            ApplicationStatus.STOPPED,
        }
    ),
    ApplicationStatus.VERIFICATION_REQUIRED: frozenset(
        {
            ApplicationStatus.SUBMITTING,
            ApplicationStatus.SUBMITTED,
            ApplicationStatus.FAILED,
            ApplicationStatus.STOPPED,
        }
    ),
    # Terminal states.
    ApplicationStatus.SUBMITTED: frozenset(),
    ApplicationStatus.FAILED: frozenset(),
    ApplicationStatus.STOPPED: frozenset(),
}


class ArtifactType(StrEnum):
    RESUME_PDF = "resume_pdf"
    RESUME_TEX = "resume_tex"
    COVER_LETTER = "cover_letter"
    SCREENSHOT = "screenshot"
    PAGE_SNAPSHOT = "page_snapshot"
    SOURCE_UPLOAD = "source_upload"


class StopReason(StrEnum):
    """Why an automated flow handed control back to the human."""

    CAPTCHA = "captcha"
    UNEXPECTED_AUTH = "unexpected_authentication"
    SUSPICIOUS_PAGE = "suspicious_page"
    PAYMENT_REQUESTED = "payment_requested"
    UNKNOWN_HIGH_IMPACT_QUESTION = "unknown_high_impact_question"
    UNEXPECTED_STRUCTURE = "unexpected_structure"
    NETWORK_UNCERTAIN = "network_uncertain"
    USER_REQUESTED = "user_requested"
