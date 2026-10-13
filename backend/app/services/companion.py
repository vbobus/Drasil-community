"""Durable, account-scoped companion jobs. Workers cannot mutate planning data."""
from __future__ import annotations

import hashlib
import hmac
import json
import secrets
from datetime import datetime, timedelta, timezone
from uuid import UUID
from zoneinfo import ZoneInfo

from fastapi import HTTPException
from sqlmodel import Session, select
from sqlalchemy.exc import IntegrityError

from app.models.companion import CompanionRun, CompanionSettings


def token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def settings(session: Session) -> CompanionSettings:
    value = session.get(CompanionSettings, 1)
    if value is None:
        # Creation is performed through the authenticated UI before pairing.
        value = CompanionSettings()
        session.add(value)
        try:
            session.commit()
            session.refresh(value)
        except IntegrityError:
            session.rollback()
            value = session.get(CompanionSettings, 1)
            if value is None:
                raise
    return value


def public_settings(value: CompanionSettings) -> dict:
    _ = value.id  # Load expired SQLAlchemy state before Pydantic serialization.
    data = value.model_dump(exclude={"worker_token_hash", "check_in_hours"})
    data["check_in_hours"] = [int(hour) for hour in value.check_in_hours.split(",") if hour]
    data["paired"] = bool(value.worker_token_hash)
    return data


def issue_worker_token(session: Session, account_id: str) -> str:
    token = f"dcw.{UUID(account_id)}.{secrets.token_urlsafe(32)}"
    value = settings(session)
    value.worker_token_hash = token_hash(token)
    value.worker_status = "awaiting_worker"
    session.add(value)
    session.commit()
    return token


def authenticate_worker(token: str):
    from app.services.account_auth import get_account, use_account
    from app.db.database import get_active_engine
    try:
        prefix, account_id, secret = token.split(".")
        if prefix != "dcw" or len(secret) != 43:
            return None
        account = get_account(str(UUID(account_id)))
    except (ValueError, TypeError):
        return None
    if account is None:
        return None
    with use_account(account), Session(get_active_engine()) as session:
        value = session.get(CompanionSettings, 1)
        if not value or not value.worker_token_hash:
            return None
        return account if hmac.compare_digest(value.worker_token_hash, token_hash(token)) else None


def serialize_run(run: CompanionRun) -> dict:
    _ = run.id
    return {**run.model_dump(exclude={"lease_hash", "lease_until", "result_json", "schedule_key"}),
            "result": json.loads(run.result_json) if run.result_json else None}


def enqueue(session: Session, prompt: str) -> CompanionRun:
    settings(session)
    value = session.exec(select(CompanionSettings).where(CompanionSettings.id == 1).with_for_update()).one()
    if not value.enabled or not value.worker_token_hash:
        raise HTTPException(409, "Connect and enable your companion first.")
    pending = session.exec(select(CompanionRun).where(CompanionRun.status.in_(["queued", "running"]))).all()
    if len(pending) >= 3:
        raise HTTPException(409, "Your companion already has work waiting. Please let it finish.")
    run = CompanionRun(prompt=prompt)
    session.add(run)
    session.commit()
    session.refresh(run)
    return run


def claim(session: Session, *, worker_status: str, memory_status: str, now: datetime | None = None) -> dict:
    now = now or datetime.utcnow()
    # Serialize claims and completion against this account's singleton on Postgres.
    value = session.exec(select(CompanionSettings).where(CompanionSettings.id == 1).with_for_update()).one()
    value.last_seen_at = now
    value.worker_status = worker_status
    value.memory_status = memory_status
    session.add(value)
    for expired in session.exec(select(CompanionRun).where(
        CompanionRun.status == "running", CompanionRun.lease_until < now
    )).all():
        expired.status = "failed"
        expired.error = "The worker stopped before finishing. You can try again."
        expired.completed_at = now
        session.add(expired)
    if not value.enabled or worker_status != "ready" or memory_status != "ready":
        session.commit()
        return {"run": None, "poll_after_seconds": 120}
    local = now.replace(tzinfo=timezone.utc).astimezone(ZoneInfo(value.time_zone))
    # A queued scheduled prompt must not become an obsolete reminder tomorrow.
    for queued in session.exec(select(CompanionRun).where(
        CompanionRun.status == "queued", CompanionRun.kind == "check_in"
    )).all():
        date_text, hour_text = queued.schedule_key.rsplit(":", 1)
        if date_text != str(local.date()) or local.hour >= int(hour_text) + 2:
            queued.status = "completed"
            queued.completed_at = now
            queued.result_json = json.dumps({"message": "", "suggested_prompt": None, "quiet": True})
            session.add(queued)
    day_start = local.replace(hour=0, minute=0, second=0, microsecond=0).astimezone(timezone.utc).replace(tzinfo=None)
    runs_today = session.exec(select(CompanionRun).where(CompanionRun.started_at >= day_start)).all()
    running = session.exec(select(CompanionRun).where(CompanionRun.status == "running")).first()
    if len(runs_today) >= value.daily_run_limit or running:
        session.commit()
        return {"run": None, "poll_after_seconds": 300}
    # A missed check-in expires after two hours; restarts never unleash old reminders.
    for hour in map(int, filter(None, value.check_in_hours.split(","))):
        if hour <= local.hour < min(hour + 2, 24):
            key = f"{local.date()}:{hour}"
            if not session.exec(select(CompanionRun).where(CompanionRun.schedule_key == key)).first():
                session.add(CompanionRun(kind="check_in", schedule_key=key, prompt=
                    "Review my current goals, commitments, and recent follow-ups. Send one useful check-in "
                    "only if there is something new or an agreed follow-up is due. Otherwise stay quiet."))
    session.flush()
    run = session.exec(select(CompanionRun).where(CompanionRun.status == "queued")
                       .order_by(CompanionRun.created_at).with_for_update()).first()
    if run is None:
        session.commit()
        return {"run": None, "poll_after_seconds": 120}
    lease = secrets.token_urlsafe(32)
    run.status = "running"
    run.started_at = now
    run.lease_until = now + timedelta(minutes=15)
    run.lease_hash = token_hash(lease)
    session.add(run)
    session.commit()
    return {"run": {**serialize_run(run), "lease": lease}, "poll_after_seconds": 5}


def validate_lease(session: Session, run_id: str, lease: str) -> CompanionRun:
    run = session.get(CompanionRun, run_id)
    if not run or run.status != "running" or not run.lease_until or run.lease_until < datetime.utcnow() or not hmac.compare_digest(run.lease_hash, token_hash(lease)):
        raise HTTPException(409, "This companion run is no longer active.")
    return run


def context(session: Session) -> dict:
    from app.models.node import StrategyNode
    from app.models.category import Category
    from app.models.calendar_block import CalendarBlock
    from app.models.preference_memory import UserPreference
    value = settings(session)
    today = datetime.now(ZoneInfo(value.time_zone)).date()
    nodes = session.exec(select(StrategyNode).order_by(StrategyNode.id).limit(2001)).all()
    # Explicitly report truncation; never imply an incomplete workspace is complete.
    blocks = session.exec(select(CalendarBlock).where(
        CalendarBlock.start_at >= datetime.combine(today - timedelta(days=7), datetime.min.time()),
        CalendarBlock.start_at < datetime.combine(today + timedelta(days=15), datetime.min.time())
    ).limit(1000)).all()
    history = session.exec(select(CompanionRun).where(CompanionRun.status == "completed")
                           .order_by(CompanionRun.created_at.desc()).limit(20)).all()
    return {"now": datetime.now(ZoneInfo(value.time_zone)).isoformat(), "time_zone": value.time_zone,
            "personal_context": value.personal_context,
            "tasks": [node.model_dump(mode="json", exclude={"image_attachments"}) for node in nodes[:2000]],
            "tasks_truncated": len(nodes) > 2000,
            "categories": [item.model_dump(mode="json") for item in session.exec(select(Category)).all()],
            "calendar": [item.model_dump(mode="json") for item in blocks],
            "preferences": [item.model_dump(mode="json") for item in session.exec(select(UserPreference).where(UserPreference.active == True)).all()],
            "recent_check_ins": [serialize_run(item) for item in reversed(history)]}
