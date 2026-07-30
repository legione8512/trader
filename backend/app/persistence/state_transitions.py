"""Audited state transitions.

Every persisted state change goes through this one function. It does three
things, in this order, and never any subset of them:

1. asks the state machine whether the transition is legal, raising if not;
2. writes an audit event recording the previous and the new state;
3. applies the change to the entity.

Doing it anywhere else would mean a state that changed without a trace, and
"every trade must be reconstructable" would stop being true the first time
someone wrote ``day.status = CLOSED`` by hand.
"""

from __future__ import annotations

import uuid
from enum import StrEnum
from typing import Any, Protocol, runtime_checkable

from app.domain.enums import AuditActor
from app.domain.state_machines import StateMachine
from app.persistence.repositories import AuditRepository


@runtime_checkable
class StatefulEntity(Protocol):
    """Anything with an identity and a mutable status.

    ``status`` is typed ``Any`` because each entity carries a different enum;
    the state machine passed alongside is what makes the pair type-correct, and
    it validates the value at runtime.
    """

    id: uuid.UUID
    status: Any


async def apply_transition[S: StrEnum](
    *,
    entity: StatefulEntity,
    machine: StateMachine[S],
    target: S,
    audit: AuditRepository,
    aggregate_type: str,
    event_type: str,
    actor: AuditActor = AuditActor.SYSTEM,
    actor_detail: str | None = None,
    reason: str | None = None,
    summary: str | None = None,
    correlation_id: uuid.UUID | None = None,
    payload: dict[str, Any] | None = None,
) -> None:
    """Move an entity to a new state, refusing anything the machine forbids.

    Raises ``InvalidTransitionError`` without touching the entity or writing an
    audit event, so a refused transition leaves no trace of having happened -
    because it did not happen.
    """
    current: S = entity.status
    machine.assert_transition(current, target)

    event_payload: dict[str, Any] = dict(payload or {})
    if reason is not None:
        event_payload["reason"] = reason

    await audit.record(
        event_type=event_type,
        actor=actor,
        actor_detail=actor_detail,
        aggregate_type=aggregate_type,
        aggregate_id=entity.id,
        correlation_id=correlation_id,
        previous_state=current.value,
        new_state=target.value,
        payload=event_payload,
        summary=summary,
    )

    entity.status = target
