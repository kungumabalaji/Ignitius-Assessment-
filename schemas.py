from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class GoalRequest(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)

    message: str = Field(min_length=3, max_length=2000)


class Resource(BaseModel):
    id: str
    title: str
    type: Literal["video", "exercise", "reading"]
    description: str


class Step(BaseModel):
    order: int
    action: str
    why: str
    resource: Resource | None = None


class CalendarResult(BaseModel):
    status: Literal["scheduled", "not_requested", "invalid_time", "failed"]
    title: str | None = None
    timestamp: str | None = None
    event_id: str | None = None
    detail: str | None = None


class GoalTrackResponse(BaseModel):
    status: Literal["complete", "partial", "needs_more_detail"]
    goal: str | None
    summary: str | None
    steps: list[Step]
    calendar: CalendarResult
    warnings: list[str] = []
