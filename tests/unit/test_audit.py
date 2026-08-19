"""Audit trail behaviour."""

from __future__ import annotations

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from packages.core import audit
from packages.core.db.models import Application, AuditEvent, Candidate, Job
from packages.core.db.types import utcnow
from packages.core.ids import new_id
from packages.core.logging import REDACTED


def _candidate(db: Session) -> Candidate:
    candidate = Candidate(id=new_id("candidate"), display_name="Test Candidate")
    db.add(candidate)
    db.flush()
    return candidate


def _job(db: Session, source_job_id: str = "req-1") -> Job:
    job = Job(
        id=new_id("job"),
        source="mock",
        source_job_id=source_job_id,
        company="Acme",
        title="Senior Backend Engineer",
        retrieved_at=utcnow(),
    )
    db.add(job)
    db.flush()
    return job


class TestAuditWriter:
    def test_event_is_written(self, db: Session) -> None:
        candidate = _candidate(db)
        audit.record(
            db,
            actor=audit.ACTOR_USER,
            action="candidate.fact_verified",
            entity_type="candidate",
            entity_id=candidate.id,
            metadata={"fact_count": 3},
        )
        db.flush()

        events = db.scalars(select(AuditEvent)).all()
        assert len(events) == 1
        assert events[0].action == "candidate.fact_verified"
        assert events[0].metadata_json == {"fact_count": 3}

    def test_sensitive_metadata_is_redacted_before_storage(self, db: Session) -> None:
        candidate = _candidate(db)
        audit.record(
            db,
            actor=audit.ACTOR_SYSTEM,
            action="application.answer_filled",
            entity_type="candidate",
            entity_id=candidate.id,
            metadata={"expected_salary": "150000", "session_cookie": "abc", "field": "salary"},
        )
        db.flush()

        stored = db.scalars(select(AuditEvent)).one()
        assert stored.metadata_json["expected_salary"] == REDACTED
        assert stored.metadata_json["session_cookie"] == REDACTED
        # Non-sensitive context is preserved so the event stays meaningful.
        assert stored.metadata_json["field"] == "salary"

    def test_audit_row_rolls_back_with_its_action(self, db: Session) -> None:
        """An audit entry must never describe a change that did not happen."""
        candidate = _candidate(db)
        db.commit()

        try:
            job = _job(db)
            db.add(Application(id=new_id("application"), candidate_id=candidate.id, job_id=job.id))
            audit.record(
                db,
                actor=audit.ACTOR_USER,
                action="application.created",
                entity_type="application",
                entity_id="app_x",
            )
            raise RuntimeError("simulated failure after the audit call")
        except RuntimeError:
            db.rollback()

        assert db.scalars(select(AuditEvent)).all() == []


class TestDatabaseConstraints:
    def test_duplicate_application_is_rejected(self, db: Session) -> None:
        """PRD asks for duplicate-submit prevention; the schema enforces it."""
        candidate = _candidate(db)
        job = _job(db)
        db.add(Application(id=new_id("application"), candidate_id=candidate.id, job_id=job.id))
        db.commit()

        db.add(Application(id=new_id("application"), candidate_id=candidate.id, job_id=job.id))
        with pytest.raises(IntegrityError):
            db.commit()
        db.rollback()

    def test_duplicate_idempotency_key_is_rejected(self, db: Session) -> None:
        candidate = _candidate(db)
        job_a, job_b = _job(db, "req-a"), _job(db, "req-b")
        db.add(
            Application(
                id=new_id("application"),
                candidate_id=candidate.id,
                job_id=job_a.id,
                idempotency_key="key-1",
            )
        )
        db.commit()

        db.add(
            Application(
                id=new_id("application"),
                candidate_id=candidate.id,
                job_id=job_b.id,
                idempotency_key="key-1",
            )
        )
        with pytest.raises(IntegrityError):
            db.commit()
        db.rollback()

    def test_foreign_keys_are_enforced(self, db: Session) -> None:
        """SQLite ignores FKs unless the pragma is set; verify ours is."""
        db.add(
            Application(id=new_id("application"), candidate_id="cand_missing", job_id="job_missing")
        )
        with pytest.raises(IntegrityError):
            db.commit()
        db.rollback()
