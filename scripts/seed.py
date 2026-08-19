"""Load local demo fixtures.

Idempotent: running it twice leaves the same state, so it is safe to wire into
``make seed`` and re-run while developing. Each milestone extends this with the
fixtures that milestone needs (resumes in M1, jobs and matches in M2).
"""

from __future__ import annotations

import sys
from pathlib import Path

# Allow `python scripts/seed.py` from the repo root without an editable install.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import select

from packages.core import audit
from packages.core.db.models import Candidate
from packages.core.db.session import session_scope
from packages.core.errors import ConflictError
from packages.core.ids import new_id
from packages.core.llm.client import build_client
from packages.core.logging import configure_logging
from packages.core.settings import get_settings
from packages.schemas.candidate import CandidatePreferences, TargetRole
from packages.schemas.enums import EmploymentType, RemoteMode
from services.candidate.service import CandidateService

DEMO_NAME = "Demo Candidate"
FIXTURE_RESUME = Path(__file__).resolve().parents[1] / "tests/fixtures/resume_sample.tex"


def demo_preferences() -> CandidatePreferences:
    """Target-role preferences. Deliberately *not* resume evidence (FR-06)."""
    return CandidatePreferences(
        target_roles=[
            TargetRole(
                title="Senior Backend Engineer",
                seniority="senior",
                keywords=["python", "distributed systems", "apis"],
            ),
            TargetRole(
                title="Machine Learning Engineer",
                seniority="senior",
                keywords=["pytorch", "computer vision", "mlops"],
            ),
        ],
        locations=["Bengaluru", "Remote (India)"],
        remote_modes=[RemoteMode.REMOTE, RemoteMode.HYBRID],
        employment_types=[EmploymentType.FULL_TIME],
        exclusions=["gambling", "surveillance tech"],
        willing_to_relocate=False,
        # Left empty on purpose: an unset authorization stays UNKNOWN rather than
        # being assumed, and the UI asks the user to confirm it.
        work_authorization={},
    )


def seed() -> int:
    """Create the demo candidate and ingest the fixture resume.

    Ingestion runs through the real pipeline, so ``make seed`` exercises extraction,
    evidence linking and fact validation end to end. With the stub provider and no
    registered fixture the model pass is skipped, and the deterministic pass still
    produces contacts and skills — which is exactly the no-API-key experience.
    """
    settings = get_settings()
    settings.ensure_dirs()

    with session_scope() as session:
        candidate = session.scalar(select(Candidate).where(Candidate.display_name == DEMO_NAME))

        if candidate is None:
            candidate = Candidate(
                id=new_id("candidate"),
                display_name=DEMO_NAME,
                preferences=demo_preferences().model_dump(mode="json"),
            )
            session.add(candidate)
            audit.record(
                session,
                actor=audit.ACTOR_SYSTEM,
                action="candidate.seeded",
                entity_type="candidate",
                entity_id=candidate.id,
                metadata={"source": "scripts/seed.py"},
            )
            session.flush()
            print(f"seeded candidate {candidate.id}")
        else:
            print(f"candidate already seeded: {candidate.id}")

        service = CandidateService(session, build_client(settings), settings)
        try:
            report = service.ingest_resume(candidate.id, FIXTURE_RESUME)
        except ConflictError as exc:
            print(f"resume already ingested ({exc.message})")
        else:
            print(
                f"ingested {FIXTURE_RESUME.name}: "
                f"{report.block_count} blocks -> {report.evidence_count} evidence records "
                f"-> {report.facts_created} facts "
                f"({report.facts_needing_review} awaiting review, {report.facts_rejected} rejected)"
            )
            if not report.llm_extraction_ran:
                print(
                    "  note: model extraction did not run (stub provider). "
                    "Set CA_LLM_PROVIDER=anthropic with CA_ANTHROPIC_API_KEY for full extraction."
                )

    print(f"database: {settings.database_url}")
    return 0


if __name__ == "__main__":
    configure_logging(get_settings().log_level)
    raise SystemExit(seed())
