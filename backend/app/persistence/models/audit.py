"""Audit trail and technical event log.

Two separate tables on purpose:

* ``audit_event`` records **decisions**: what the system chose to do and why.
  Every trade must be reconstructable from these rows alone.
* ``system_event`` records **technical facts**: reconnects, rate limits,
  degraded health. Operationally important, but not part of the decision trail.

Mixing them would make the decision history impossible to read at the moment it
matters most, and would let log noise bury an audit record.

Both are append-only. Neither carries ``updated_at``: a record that can be
modified is not an audit trail.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import Index, String, Text, Uuid
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.domain.enums import AuditActor, EventSeverity, HealthStatus
from app.persistence.base import Base
from app.persistence.mixins import CreatedAtMixin, UuidPrimaryKeyMixin
from app.persistence.types import UtcDateTime, enum_column


class AuditEvent(UuidPrimaryKeyMixin, CreatedAtMixin, Base):
    """An immutable record of a decision or a state transition."""

    __tablename__ = "audit_event"
    __table_args__ = (
        # Reconstructing one trade: find every event for that aggregate.
        Index("ix_audit_event_aggregate", "aggregate_type", "aggregate_id"),
        # Reading a day in order.
        Index("ix_audit_event_occurred_at", "occurred_at"),
        # Following one decision across aggregates: signal to risk assessment
        # to order intent to fill.
        Index("ix_audit_event_correlation_id", "correlation_id"),
        Index("ix_audit_event_event_type", "event_type"),
    )

    #: When the recorded thing happened, which is not always when the row was
    #: written. A reconciliation performed at 12:05 may record a fill that
    #: happened at 11:58.
    occurred_at: Mapped[datetime] = mapped_column(UtcDateTime, nullable=False)

    #: Machine-readable event name, for example ORDER_INTENT_RECORDED.
    event_type: Mapped[str] = mapped_column(String(64), nullable=False)

    #: What the event is about, for example "Order" and its id.
    aggregate_type: Mapped[str | None] = mapped_column(String(64), nullable=True)
    aggregate_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)

    actor: Mapped[AuditActor] = mapped_column(
        enum_column(AuditActor, name="audit_actor"),
        nullable=False,
        default=AuditActor.SYSTEM,
    )
    #: Free-form detail about the actor, for example a strategy version or an
    #: operator name. Never a credential.
    actor_detail: Mapped[str | None] = mapped_column(String(128), nullable=True)

    #: Ties every event produced by one decision chain together.
    correlation_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)

    #: State transition, when the event is one.
    previous_state: Mapped[str | None] = mapped_column(String(48), nullable=True)
    new_state: Mapped[str | None] = mapped_column(String(48), nullable=True)

    #: Structured detail. Passed through the same secret masker as the logs
    #: before being written: an exchange response can quote a signed URL.
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)

    #: Human-readable explanation. Every refusal must be explainable to an
    #: operator without reading code.
    summary: Mapped[str | None] = mapped_column(Text, nullable=True)

    def __repr__(self) -> str:
        return f"<AuditEvent {self.event_type} {self.aggregate_type}:{self.aggregate_id}>"


class SystemEvent(UuidPrimaryKeyMixin, CreatedAtMixin, Base):
    """A technical or operational event."""

    __tablename__ = "system_event"
    __table_args__ = (
        Index("ix_system_event_occurred_at", "occurred_at"),
        Index("ix_system_event_severity", "severity"),
        Index("ix_system_event_category", "category"),
    )

    occurred_at: Mapped[datetime] = mapped_column(UtcDateTime, nullable=False)
    severity: Mapped[EventSeverity] = mapped_column(
        enum_column(EventSeverity, name="event_severity"),
        nullable=False,
    )
    #: Coarse grouping, for example MARKET_DATA, EXCHANGE, DATABASE, SCHEDULER.
    category: Mapped[str] = mapped_column(String(64), nullable=False)
    message: Mapped[str] = mapped_column(Text, nullable=False)
    detail: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)

    #: Health state at the time, when the event changed it.
    health_status: Mapped[HealthStatus | None] = mapped_column(
        enum_column(HealthStatus, name="health_status"),
        nullable=True,
    )

    def __repr__(self) -> str:
        return f"<SystemEvent {self.severity} {self.category}>"
