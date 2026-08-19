"""Audit trail.

CLAUDE.md rule 8: important actions are auditable. Every consequential action
(fact verified, resume version created, edit applied, application approved,
submission attempted, safety stop) writes one row here.

Two properties matter:
* append-only — nothing in the application updates or deletes these rows;
* redacted — metadata passes through the same filter as the logs, so an audit
  entry can never become the place a token or salary figure leaks.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy.orm import Session

from packages.core.db.models import AuditEvent
from packages.core.ids import new_id
from packages.core.logging import get_logger, redact

logger = get_logger(__name__)

#: Actor conventions. Anything else should be a named agent task.
ACTOR_USER = "user"
ACTOR_SYSTEM = "system"


def record(
    session: Session,
    *,
    actor: str,
    action: str,
    entity_type: str,
    entity_id: str,
    metadata: dict[str, Any] | None = None,
) -> AuditEvent:
    """Append an audit event to the current transaction.

    Deliberately does not commit: the audit row lands atomically with the change
    it describes, so there is never an audit entry for a rolled-back action.
    """
    safe_metadata: dict[str, Any] = redact(metadata or {})
    event = AuditEvent(
        id=new_id("audit_event"),
        actor=actor,
        action=action,
        entity_type=entity_type,
        entity_id=entity_id,
        metadata_json=safe_metadata,
    )
    session.add(event)
    logger.info(
        "audit",
        extra={
            "action": action,
            "actor": actor,
            "entity_type": entity_type,
            "entity_id": entity_id,
        },
    )
    return event
