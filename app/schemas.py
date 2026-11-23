import uuid
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class JobCreate(BaseModel):
    type: str = Field(pattern=r"^[a-z][a-z0-9._-]{0,63}$")
    payload: dict = Field(default_factory=dict)
    priority: Literal["high", "normal", "low"] = "normal"
    run_at: datetime | None = None
    max_retries: int = Field(default=3, ge=0, le=10)


class JobView(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    type: str
    payload: dict
    priority: str
    status: str
    run_at: datetime
    max_retries: int
    attempt_count: int
    last_error: str | None
    result: dict | None
    worker_id: uuid.UUID | None
    created_at: datetime
    updated_at: datetime
    started_at: datetime | None
    finished_at: datetime | None


class JobList(BaseModel):
    items: list[JobView]
    total: int
    limit: int
    offset: int


class LogView(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    job_id: uuid.UUID
    worker_id: uuid.UUID | None
    attempt: int
    level: str
    message: str
    created_at: datetime


class LogList(BaseModel):
    items: list[LogView]


class JobTypeView(BaseModel):
    name: str
    description: str


class WorkerView(BaseModel):
    id: uuid.UUID
    name: str
    hostname: str
    pid: int
    online: bool
    last_heartbeat_at: datetime
    registered_at: datetime
    stopped_at: datetime | None


class WorkerList(BaseModel):
    items: list[WorkerView]
