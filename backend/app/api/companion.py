"""Browser controls and a deliberately narrow, separately authenticated worker API."""
import json
from datetime import datetime
from typing import Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field, field_validator
from sqlmodel import Session, select

from app.db.database import get_session
from app.models.companion import CompanionRun, CompanionSettings
from app.services import companion as service
from app.services.account_auth import active_account

router = APIRouter(prefix="/api/v1/companion", tags=["Companion"])


class SettingsUpdate(BaseModel):
    enabled: bool
    time_zone: str = "America/Vancouver"
    check_in_hours: list[int] = Field(default_factory=lambda: [9, 18], max_length=3)
    daily_run_limit: int = Field(default=6, ge=1, le=12)
    personal_context: str = Field(default="", max_length=12000)

    @field_validator("time_zone")
    @classmethod
    def valid_zone(cls, value):
        try:
            ZoneInfo(value)
        except (ZoneInfoNotFoundError, ValueError):
            raise ValueError("Choose a valid time zone.")
        return value

    @field_validator("check_in_hours")
    @classmethod
    def valid_hours(cls, value):
        if any(hour < 0 or hour > 23 for hour in value):
            raise ValueError("Hours must be between 0 and 23.")
        return sorted(set(value))


class Message(BaseModel):
    prompt: str = Field(min_length=1, max_length=4000)

    @field_validator("prompt")
    @classmethod
    def nonblank(cls, value):
        if not value.strip():
            raise ValueError("Enter a message.")
        return value.strip()


class WorkerHealth(BaseModel):
    worker_status: Literal["ready", "needs_login", "error"]
    memory_status: Literal["ready", "unavailable", "needs_login"]


class ContextRequest(BaseModel):
    run_id: str
    lease: str = Field(min_length=20, max_length=100)


class Completion(BaseModel):
    lease: str = Field(min_length=20, max_length=100)
    status: Literal["completed", "failed"]
    message: str = Field(default="", max_length=12000)
    suggested_prompt: str | None = Field(default=None, max_length=4000)
    quiet: bool = False
    error_code: Literal["auth", "memory", "provider", "timeout", "invalid_output"] | None = None


def worker_only(request: Request):
    if not getattr(request.state, "companion_worker", False):
        raise HTTPException(403, "A companion worker credential is required.")


@router.get("/settings")
def get_settings(session: Session = Depends(get_session)):
    return service.public_settings(service.settings(session))


@router.put("/settings")
def update_settings(body: SettingsUpdate, session: Session = Depends(get_session)):
    service.settings(session)
    value = session.exec(select(CompanionSettings).where(CompanionSettings.id == 1).with_for_update()).one()
    for key, item in body.model_dump().items():
        setattr(value, key, ",".join(map(str, item)) if key == "check_in_hours" else item)
    if not value.enabled:
        for run in session.exec(select(CompanionRun).where(CompanionRun.status.in_(["queued", "running"]))).all():
            run.status = "failed"
            run.error = "Stopped because the companion was paused."
            run.completed_at = datetime.utcnow()
            session.add(run)
    session.add(value)
    session.commit()
    return service.public_settings(value)


@router.post("/pair")
def pair(session: Session = Depends(get_session)):
    return {"token": service.issue_worker_token(session, active_account().id)}


@router.delete("/pair")
def unpair(session: Session = Depends(get_session)):
    service.settings(session)
    value = session.exec(select(CompanionSettings).where(CompanionSettings.id == 1).with_for_update()).one()
    value.worker_token_hash = ""
    value.enabled = False
    value.worker_status = "not_connected"
    value.memory_status = "not_connected"
    for run in session.exec(select(CompanionRun).where(CompanionRun.status.in_(["queued", "running"]))).all():
        run.status = "failed"
        run.error = "Worker disconnected. Reconnect before trying again."
        run.completed_at = datetime.utcnow()
        session.add(run)
    session.add(value)
    session.commit()
    return service.public_settings(value)


@router.get("/runs")
def list_runs(session: Session = Depends(get_session)):
    return [service.serialize_run(run) for run in session.exec(select(CompanionRun)
            .order_by(CompanionRun.created_at.desc()).limit(50)).all()]


@router.post("/runs", status_code=202)
def create_run(body: Message, session: Session = Depends(get_session)):
    return service.serialize_run(service.enqueue(session, body.prompt))


@router.post("/runs/{run_id}/dismiss")
def dismiss(run_id: str, session: Session = Depends(get_session)):
    run = session.get(CompanionRun, run_id)
    if not run:
        raise HTTPException(404, "Check-in not found.")
    run.dismissed = True
    session.add(run)
    session.commit()
    return service.serialize_run(run)


@router.post("/worker/claim", dependencies=[Depends(worker_only)])
def claim(body: WorkerHealth, session: Session = Depends(get_session)):
    return service.claim(session, **body.model_dump())


@router.post("/worker/context", dependencies=[Depends(worker_only)])
def context(body: ContextRequest, session: Session = Depends(get_session)):
    service.validate_lease(session, body.run_id, body.lease)
    return service.context(session)


@router.post("/worker/runs/{run_id}/complete", dependencies=[Depends(worker_only)])
def complete(run_id: str, body: Completion, session: Session = Depends(get_session)):
    session.exec(select(CompanionSettings).where(CompanionSettings.id == 1).with_for_update()).one()
    run = session.get(CompanionRun, run_id)
    # Same lease may acknowledge a lost successful response; never reapply a result.
    if run and run.status in {"completed", "failed"} and run.lease_hash and service.hmac.compare_digest(run.lease_hash, service.token_hash(body.lease)):
        return service.serialize_run(run)
    run = service.validate_lease(session, run_id, body.lease)
    if body.status == "completed" and not body.quiet and not body.message.strip():
        raise HTTPException(422, "A visible check-in needs a message.")
    if body.quiet and run.kind != "check_in":
        raise HTTPException(422, "A direct message needs a response.")
    run.status = body.status
    run.completed_at = datetime.utcnow()
    if body.status == "completed":
        run.result_json = json.dumps({"message": body.message, "suggested_prompt": body.suggested_prompt, "quiet": body.quiet})
    else:
        run.error = {"auth": "Reconnect the worker's ChatGPT subscription.",
                     "memory": "Hindsight is unavailable. Check the memory service, then try again.",
                     "timeout": "The companion took too long. Try a smaller request.",
                     "invalid_output": "The companion could not prepare a usable response. Please retry."}.get(body.error_code, "The companion could not finish. Check the worker connection and try again.")
    session.add(run)
    session.commit()
    return service.serialize_run(run)
