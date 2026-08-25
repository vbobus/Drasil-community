"""Shared optimistic revision advancement for preserved ORM mutation paths."""

from __future__ import annotations

from typing import Any

from sqlalchemy import event, inspect, update
from sqlalchemy.orm import object_session


EXPLICIT_REVISION_CONTROL = "agent_kernel_explicit_revision_control"


class EntityRevisionConflict(RuntimeError):
    """A protected entity changed after an ORM writer loaded it."""


def register_revisioned_model(model: type[Any]) -> None:
    """CAS-advance ``revision`` before an ordinary ORM update.

    The native transaction executor performs its own approved-revision CAS and
    marks its Session with ``EXPLICIT_REVISION_CONTROL``. All preserved writers
    use this hook so they cannot mutate a protected row without invalidating an
    already-approved native snapshot.
    """

    @event.listens_for(model, "before_update", propagate=True)
    def _advance_revision(_mapper: Any, connection: Any, target: Any) -> None:
        session = object_session(target)
        if session is None or session.info.get(EXPLICIT_REVISION_CONTROL):
            return
        if not session.is_modified(target, include_collections=False):
            return

        state = inspect(target)
        history = state.attrs.revision.history
        expected = int(history.deleted[0]) if history.deleted else int(target.revision)
        desired = int(target.revision) if history.has_changes() else expected + 1
        if desired != expected + 1:
            raise EntityRevisionConflict(
                f"revision must advance exactly once from {expected}"
            )

        identity = state.identity
        if identity is None:
            return
        table = model.__table__
        predicates = [
            column == value
            for column, value in zip(model.__mapper__.primary_key, identity, strict=True)
        ]
        result = connection.execute(
            update(table)
            .where(*predicates, table.c.revision == expected)
            .values(revision=desired)
        )
        if result.rowcount != 1:
            raise EntityRevisionConflict(
                f"stale {table.name} revision: expected {expected}"
            )
        target.revision = desired
