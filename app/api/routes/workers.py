from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.database import get_db
from app.schemas import WorkerList, WorkerView
from app.services.workers import is_online, list_workers

router = APIRouter(prefix="/workers", tags=["workers"])


@router.get("", response_model=WorkerList)
def get_workers(db: Session = Depends(get_db)):
    """List workers registered by the worker process, with a live heartbeat flag."""
    return WorkerList(
        items=[
            WorkerView(
                id=worker.id,
                name=worker.name,
                hostname=worker.hostname,
                pid=worker.pid,
                online=is_online(worker),
                last_heartbeat_at=worker.last_heartbeat_at,
                registered_at=worker.registered_at,
                stopped_at=worker.stopped_at,
            )
            for worker in list_workers(db)
        ]
    )
