"""One authenticated read for the primary workspace; no chat/provider work."""
from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, ConfigDict, alias_generators
from sqlmodel import Session

from app.api.categories import CategoryRead, list_categories
from app.api.nodes import StrategyNodeRead, read_workspace_nodes
from app.api.calendar_blocks import CalendarBlockRead, list_calendar_blocks, list_scheduled_task_ids
from app.db.database import get_session
from app.core.request_timing import timed

router = APIRouter(prefix="/api/v1/workspace", tags=["Workspace"])


class WorkspaceRead(BaseModel):
    model_config = ConfigDict(alias_generator=alias_generators.to_camel, populate_by_name=True)
    categories: list[CategoryRead]
    nodes: list[StrategyNodeRead]
    blocks: list[CalendarBlockRead]
    scheduled_task_ids: list[int]
    start_at: str
    end_at: str


@router.get("", response_model=WorkspaceRead)
def read_workspace(start: str = Query(...), end: str = Query(...), session: Session = Depends(get_session)):
    from datetime import timedelta
    from app.api.calendar_blocks import parse_range_datetime, assert_valid_range
    from fastapi import HTTPException
    start_at, end_at = parse_range_datetime(start, "start"), parse_range_datetime(end, "end")
    assert_valid_range(start_at, end_at)
    if start_at.tzinfo or end_at.tzinfo or end_at - start_at > timedelta(days=62):
        raise HTTPException(400, "Use a local calendar range of at most 62 days.")
    with timed("workspace"):
        return WorkspaceRead(categories=[CategoryRead.model_validate(c) for c in list_categories(session)],
            nodes=read_workspace_nodes(session), blocks=list_calendar_blocks(start, end, session),
            scheduled_task_ids=list_scheduled_task_ids(session), start_at=start, end_at=end)
