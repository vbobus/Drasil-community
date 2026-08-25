"""
Session-scoped Strategist entity memory helpers.
"""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any

from sqlmodel import Session, select

from app.models.node import StrategyNode
from app.models.agent_execution import AgentChangeSetExecutionRecord
from app.models.strategist_session_entities import StrategistSessionEntity
from app.models.strategist_turn_mentions import StrategistTurnMention
from app.models.recurrence_series import RecurrenceSeries

MEMORY_DECAY = 0.72
MEMORY_EVICT_THRESHOLD = 0.18
MAX_WORKING_SET = 8


def _verified_execution_task_ids(
    db: Session,
    strategist_session_id: str,
    *,
    execution_limit: int = 4,
) -> list[int]:
    """Recover task continuity from authoritative, verified native executions."""

    records = list(
        db.exec(
            select(AgentChangeSetExecutionRecord)
            .where(
                AgentChangeSetExecutionRecord.session_id == strategist_session_id,
                AgentChangeSetExecutionRecord.status == "applied",
            )
            .order_by(AgentChangeSetExecutionRecord.updated_at.desc())
            .limit(max(1, execution_limit))
        ).all()
    )
    task_ids: list[int] = []

    def add_ref(raw_ref: Any) -> None:
        if not isinstance(raw_ref, dict) or str(raw_ref.get("kind") or "") != "task":
            return
        entity_id = str(raw_ref.get("entity_id") or raw_ref.get("entityId") or "")
        if not entity_id.startswith("task:"):
            return
        try:
            task_id = int(entity_id.removeprefix("task:"))
        except ValueError:
            return
        if task_id not in task_ids:
            task_ids.append(task_id)

    for record in records:
        try:
            result = json.loads(record.result_json)
        except (TypeError, json.JSONDecodeError):
            continue
        if not isinstance(result, dict) or str(result.get("status") or "") != "applied":
            continue
        for binding in result.get("bindings", []):
            if isinstance(binding, dict):
                add_ref(binding.get("entity_ref", binding.get("entityRef")))
        for fact in result.get("facts", []):
            if (
                isinstance(fact, dict)
                and str(fact.get("disposition") or "") == "present"
            ):
                add_ref(fact.get("entity_ref", fact.get("entityRef")))
    return task_ids[:MAX_WORKING_SET]


def decay_session_entities(db: Session, strategist_session_id: str) -> None:
    entities = list(
        db.exec(
            select(StrategistSessionEntity).where(
                StrategistSessionEntity.session_id == strategist_session_id
            )
        ).all()
    )
    for entity in entities:
        entity.salience = round(max(0.0, entity.salience * MEMORY_DECAY), 4)
        if entity.salience < MEMORY_EVICT_THRESHOLD:
            db.delete(entity)
        else:
            db.add(entity)


def load_session_working_set(
    db: Session,
    strategist_session_id: str | None,
) -> list[StrategistSessionEntity]:
    if not strategist_session_id:
        return []
    return list(
        db.exec(
            select(StrategistSessionEntity)
            .where(StrategistSessionEntity.session_id == strategist_session_id)
            .order_by(
                StrategistSessionEntity.salience.desc(),
                StrategistSessionEntity.last_mentioned_at.desc(),
            )
        ).all()
    )


def build_working_set_snapshot(
    *,
    db: Session,
    strategist_session_id: str | None,
) -> list[dict[str, Any]]:
    if not strategist_session_id:
        return []

    entities = load_session_working_set(db, strategist_session_id)
    applied_task_ids = _verified_execution_task_ids(db, strategist_session_id)
    node_ids = list(
        dict.fromkeys(
            [*applied_task_ids, *(entity.node_id for entity in entities)]
        )
    )[:MAX_WORKING_SET]
    node_lookup = {
        node.id: node
        for node in db.exec(select(StrategyNode).where(StrategyNode.id.in_(node_ids))).all()
        if node.id is not None
    }
    recurrence_by_task: dict[int, list[RecurrenceSeries]] = {}
    if node_ids:
        for series in db.exec(
            select(RecurrenceSeries).where(
                RecurrenceSeries.task_id.in_(node_ids),
                RecurrenceSeries.status == "active",
            )
        ).all():
            recurrence_by_task.setdefault(series.task_id, []).append(series)
    payload: list[dict[str, Any]] = []

    def attach_recurrence(item: dict[str, Any], node_id: int) -> None:
        recurrence = sorted(
            recurrence_by_task.get(node_id, []),
            key=lambda value: (value.created_at, value.id),
            reverse=True,
        )[:4]
        if recurrence:
            item["recurrence_series"] = [
                {
                    "series_ref": f"recurrence_series:{series.id}",
                    "revision": series.revision,
                    "frequency": series.frequency,
                    "interval": series.interval,
                    "days_of_week": json.loads(series.days_of_week_json or "[]"),
                    "month_days": json.loads(series.month_days_json or "[]"),
                    "start_date": series.start_date.isoformat(),
                    "end_date": series.end_date.isoformat() if series.end_date else None,
                    "occurrence_count": series.occurrence_limit,
                    "preferred_start_time": series.preferred_start_time,
                    "duration_minutes": series.duration_minutes,
                    "timezone": series.timezone,
                }
                for series in recurrence
            ]

    applied_task_id_set = set(applied_task_ids)
    for node_id in applied_task_ids:
        node = node_lookup.get(node_id)
        if node is None:
            continue
        item = {
            "node_id": node_id,
            "title": node.title,
            "role": "primary",
            "salience": 1.0,
            "source": "verified_change_set_execution",
            "priority": node.priority,
            "deadline_at": node.deadline_at,
            "status": node.status,
        }
        attach_recurrence(item, node_id)
        payload.append(item)

    for entity in entities:
        if entity.node_id in applied_task_id_set or entity.node_id not in node_lookup:
            continue
        node = node_lookup.get(entity.node_id)
        if node is None:
            continue
        item: dict[str, Any] = {
                "node_id": entity.node_id,
                "title": node.title,
                "role": entity.role,
                "salience": entity.salience,
                "source": entity.source,
                "priority": node.priority,
                "deadline_at": node.deadline_at,
                "status": node.status,
            }
        attach_recurrence(item, entity.node_id)
        payload.append(item)
    return payload[:MAX_WORKING_SET]


def persist_turn_memory(
    db: Session,
    *,
    strategist_session_id: str | None,
    turn_id: str,
    resolved_entities: list[dict[str, Any]],
) -> None:
    if not strategist_session_id:
        return

    decay_session_entities(db, strategist_session_id)
    now = datetime.utcnow()

    for entity in resolved_entities:
        node_id = entity.get("node_id")
        if not isinstance(node_id, int):
            continue
        role = str(entity.get("role") or "related").strip().lower() or "related"
        confidence = float(entity.get("confidence") or 0.0)
        mention_text = str(entity.get("mention_text") or entity.get("title") or "").strip()
        source = str(entity.get("source") or "retrieval").strip() or "retrieval"

        current = db.exec(
            select(StrategistSessionEntity).where(
                StrategistSessionEntity.session_id == strategist_session_id,
                StrategistSessionEntity.node_id == node_id,
                StrategistSessionEntity.role == role,
            )
        ).first()
        if current is None:
            current = StrategistSessionEntity(
                session_id=strategist_session_id,
                node_id=node_id,
                role=role,
                salience=max(0.35, min(1.0, confidence)),
                mention_count=1,
                last_mentioned_at=now,
                source=source,
            )
        else:
            current.salience = round(
                min(1.0, max(current.salience, 0.18) + max(0.12, confidence * 0.45)),
                4,
            )
            current.mention_count += 1
            current.last_mentioned_at = now
            current.source = source
        db.add(current)
        db.add(
            StrategistTurnMention(
                session_id=strategist_session_id,
                turn_id=turn_id,
                node_id=node_id,
                role=role,
                mention_text=mention_text,
                confidence=confidence,
                source=source,
            )
        )

    retained = load_session_working_set(db, strategist_session_id)
    for stale in retained[MAX_WORKING_SET:]:
        db.delete(stale)

    db.commit()
