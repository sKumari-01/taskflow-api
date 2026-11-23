"""Worker registration and heartbeat."""

from __future__ import annotations

import os
import socket
import uuid
from datetime import timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.models import Worker, as_utc, utcnow


def register_worker(db: Session, name: str | None = None) -> Worker:
    settings = get_settings()
    hostname = socket.gethostname()
    worker = Worker(
        id=uuid.uuid4(),
        name=(name or settings.worker_name or hostname)[:128],
        hostname=hostname[:255],
        pid=os.getpid(),
        last_heartbeat_at=utcnow(),
        registered_at=utcnow(),
    )
    db.add(worker)
    db.commit()
    db.refresh(worker)
    return worker


def heartbeat(db: Session, worker_id: uuid.UUID) -> None:
    worker = db.get(Worker, worker_id)
    if worker is None:
        return
    worker.last_heartbeat_at = utcnow()
    worker.stopped_at = None
    db.commit()


def mark_offline(db: Session, worker_id: uuid.UUID) -> None:
    worker = db.get(Worker, worker_id)
    if worker is None:
        return
    worker.stopped_at = utcnow()
    db.commit()


def is_online(worker: Worker) -> bool:
    if worker.stopped_at is not None:
        return False
    cutoff = utcnow() - timedelta(seconds=get_settings().worker_stale_seconds)
    return as_utc(worker.last_heartbeat_at) >= cutoff


def list_workers(db: Session) -> list[Worker]:
    return list(db.scalars(select(Worker).order_by(Worker.registered_at.desc())).all())
