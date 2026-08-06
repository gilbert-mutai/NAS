"""Writing the audit trail.

Two decisions here are load-bearing, and both are about failure.

**Audit entries commit on their own session, not the request's.** A `POST /sync`
rejected with 409 because a run is already in progress raises, so the request
transaction rolls back — and an audit row written on that session would vanish with
it. The rejected attempt is exactly the kind of thing the trail exists to hold, so
it gets its own transaction. Same reasoning as ``ApiKeyRepository.mark_used``.

**A failed audit write does not fail the operation.** By the time an entry is
written the switches have already been polled: raising here would return 500 for
work that actually succeeded, and the caller's retry would poll them again. So the
write is best-effort, and a failure is logged at ``error`` with the whole entry
inline — the event survives in the structured log, which is shipped to journald;
only its queryable form is lost. Silence would be the unacceptable outcome, not
degradation.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime
from typing import Any

import structlog
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from nas.domain.entities import MAX_ACTOR_LENGTH, AuditEntry
from nas.domain.enums import AuditAction, AuditOutcome
from nas.repositories.audit import SqlAlchemyAuditRepository

logger = structlog.get_logger(__name__)

# Anything outside printable ASCII plus common name characters is dropped. The
# actor lands in log lines and in a report, so a newline or an ANSI escape in it is
# a log-forging primitive, not a name.
_UNSAFE_ACTOR_CHARS = re.compile(r"[^\w.@+\- ]", flags=re.UNICODE)


def sanitize_actor(raw: str | None) -> str | None:
    """Clean a caller-supplied actor identity, or return None if there is nothing usable.

    Pure, so it is tested directly against hostile input rather than through a
    request. Truncation is silent and deliberate: an over-long actor is a bug or a
    probe, and rejecting the whole request over it would break a sync for a
    cosmetic reason.
    """
    if raw is None:
        return None
    cleaned = _UNSAFE_ACTOR_CHARS.sub("", raw).strip()
    if not cleaned:
        return None
    return cleaned[:MAX_ACTOR_LENGTH]


class AuditService:
    """Appends audit entries. Cannot amend or remove them."""

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def record(
        self,
        *,
        action: AuditAction,
        outcome: AuditOutcome,
        api_key_id: int | None = None,
        api_key_name: str | None = None,
        actor: str | None = None,
        source_ip: str | None = None,
        correlation_id: str | None = None,
        target_type: str | None = None,
        target_id: str | None = None,
        detail: dict[str, Any] | None = None,
        occurred_at: datetime | None = None,
    ) -> AuditEntry | None:
        """Append an entry. Returns None if the write failed, never raises.

        ``actor`` is sanitised here rather than at the edge so no call path can
        bypass it.
        """
        entry = AuditEntry(
            action=action,
            outcome=outcome,
            occurred_at=occurred_at or datetime.now(UTC),
            api_key_id=api_key_id,
            api_key_name=api_key_name,
            actor=sanitize_actor(actor),
            source_ip=source_ip,
            correlation_id=correlation_id,
            target_type=target_type,
            target_id=target_id,
            detail=detail,
        )
        try:
            async with self._session_factory() as session:
                repository = SqlAlchemyAuditRepository(session)
                recorded = await repository.record(entry)
                await session.commit()
        except Exception as exc:
            # Never propagate. See the module docstring: the operation being audited
            # has already happened, so failing it now would be worse than a gap.
            # Everything the row would have held goes into the log line instead.
            logger.error(
                "audit_write_failed",
                error=str(exc),
                audit_action=entry.action.value,
                audit_outcome=entry.outcome.value,
                audit_actor=entry.actor,
                audit_api_key_name=entry.api_key_name,
                audit_source_ip=entry.source_ip,
                audit_correlation_id=entry.correlation_id,
                audit_target_type=entry.target_type,
                audit_target_id=entry.target_id,
                audit_detail=entry.detail,
                audit_occurred_at=entry.occurred_at.isoformat(),
            )
            return None

        logger.info(
            "audit_recorded",
            audit_id=recorded.id,
            audit_action=recorded.action.value,
            audit_outcome=recorded.outcome.value,
            attribution=recorded.attribution,
        )
        return recorded
