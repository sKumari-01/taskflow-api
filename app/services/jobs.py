"""Submit, claim, retry, and finish jobs."""

from __future__ import annotations

import json
import logging
import uuid
from datetime import timedelta

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from app.config import get_settings
from app.models import Job, JobLog, JobStatus, Worker, as_utc, utcnow
from app.services.handlers import JobContext, get_job_type, validate_payload
from app.services.queue import JobQueue, get_queue

logger = logging.getLogger(__name__)

CANCELLABLE = {JobStatus.scheduled.value, JobStatus.queued.value}
RETRYABLE = {JobStatus.failed.value, JobStatus.cancelled.value}


class JobError(Exception):
    def __init__(self, detail: str, status_code: int = 400):
        self.detail = detail
        self.status_code = status_code
        super().__init__(detail)


class ExecutionLog(JobContext):
    def __init__(self, db: Session, job: Job, worker_id: uuid.UUID):
        self.db = db
        self.job = job
        self.worker_id = worker_id

    def log(self, message: str, level: str = "info") -> None:
        add_log(
            self.db,
            job_id=self.job.id,
            message=message,
            level=level,
            worker_id=self.worker_id,
            attempt=self.job.attempt_count,
        )
        self.db.commit()


def submit_job(
    db: Session,
    queue: JobQueue,
    *,
    job_type: str,
    payload: dict,
    priority: str,
    run_at,
    max_retries: int,
) -> Job:
    if get_job_type(job_type) is None:
        raise JobError(f"Unknown job type: {job_type}. See /api/v1/jobs/types.")
    try:
        encoded = json.dumps(payload).encode()
    except (TypeError, ValueError) as exc:
        raise JobError("payload must be JSON", 422) from exc
    if len(encoded) > get_settings().max_payload_bytes:
        raise JobError("payload is too large", 422)
    try:
        validate_payload(job_type, payload)
    except ValueError as exc:
        raise JobError(str(exc), 422) from exc

    now = utcnow()
    due_at = run_at or now
    if due_at.tzinfo is None:
        due_at = due_at.replace(tzinfo=now.tzinfo)
    due = due_at <= now
    job = Job(
        id=uuid.uuid4(),
        type=job_type,
        payload=payload,
        priority=priority,
        status=JobStatus.queued.value if due else JobStatus.scheduled.value,
        run_at=due_at,
        max_retries=max_retries,
        attempt_count=0,
        dispatched=False,
        created_at=now,
        updated_at=now,
    )
    db.add(job)
    add_log(db, job_id=job.id, message=f"Accepted {job_type} job", attempt=0)
    db.commit()
    if job.status == JobStatus.queued.value:
        dispatch_queued(db, queue)
        db.refresh(job)
    return job


def cancel_job(db: Session, queue: JobQueue, job_id: uuid.UUID) -> Job:
    job = require_job(db, job_id)
    if job.status not in CANCELLABLE:
        raise JobError(f"Only scheduled or queued jobs can be cancelled. Current status: {job.status}.", 409)
    now = utcnow()
    job.status = JobStatus.cancelled.value
    job.finished_at = now
    job.updated_at = now
    job.dispatched = False
    add_log(db, job_id=job.id, message="Cancelled before execution", attempt=job.attempt_count)
    db.commit()
    queue.remove(str(job.id), job.priority)
    db.refresh(job)
    return job


def retry_job(db: Session, queue: JobQueue, job_id: uuid.UUID) -> Job:
    job = require_job(db, job_id)
    if job.status not in RETRYABLE:
        raise JobError(f"Only failed or cancelled jobs can be retried. Current status: {job.status}.", 409)
    now = utcnow()
    job.status = JobStatus.queued.value
    job.run_at = now
    job.attempt_count = 0
    job.last_error = None
    job.result = None
    job.worker_id = None
    job.started_at = None
    job.finished_at = None
    job.dispatched = False
    job.updated_at = now
    add_log(db, job_id=job.id, message="Manual retry queued", attempt=0)
    db.commit()
    dispatch_queued(db, queue)
    db.refresh(job)
    return job


def promote_due_jobs(db: Session, queue: JobQueue) -> int:
    now = utcnow()
    upcoming = list(
        db.scalars(
            select(Job).where(Job.status == JobStatus.scheduled.value).order_by(Job.run_at).limit(100)
        ).all()
    )
    jobs = [job for job in upcoming if as_utc(job.run_at) <= now]
    if not jobs:
        return 0
    for job in jobs:
        job.status = JobStatus.queued.value
        job.dispatched = False
        job.updated_at = now
        add_log(db, job_id=job.id, message="Scheduled job is due", attempt=job.attempt_count)
    db.commit()
    return dispatch_queued(db, queue)


def dispatch_queued(db: Session, queue: JobQueue) -> int:
    jobs = list(
        db.scalars(select(Job).where(Job.status == JobStatus.queued.value, Job.dispatched.is_(False))).all()
    )
    sent = 0
    for job in jobs:
        queue.enqueue(str(job.id), job.priority)
        job.dispatched = True
        job.updated_at = utcnow()
        sent += 1
    if sent:
        db.commit()
    return sent


def execute_job(job_id: uuid.UUID, worker_id: uuid.UUID) -> None:
    from app.database import SessionLocal, init_engine

    init_engine()
    db = SessionLocal()
    try:
        now = utcnow()
        db.execute(
            update(Job)
            .where(Job.id == job_id, Job.status == JobStatus.queued.value)
            .values(
                status=JobStatus.running.value,
                worker_id=worker_id,
                started_at=now,
                updated_at=now,
                attempt_count=Job.attempt_count + 1,
            )
        )
        db.commit()
        job = db.get(Job, job_id)
        if job is None or job.status != JobStatus.running.value or job.worker_id != worker_id:
            return
        ctx = ExecutionLog(db, job, worker_id)
        ctx.log(f"Attempt {job.attempt_count} started")
        try:
            job_type = get_job_type(job.type)
            if job_type is None:
                raise RuntimeError(f"Unknown job type: {job.type}")
            output = job_type.handler(job.payload or {}, ctx)
            if not isinstance(output, dict):
                raise RuntimeError("Job handler must return an object")
        except Exception as exc:
            db.rollback()
            _finish_failure(job_id, worker_id, exc)
            return
        finished = utcnow()
        job.status = JobStatus.succeeded.value
        job.result = output
        job.last_error = None
        job.finished_at = finished
        job.updated_at = finished
        ctx.log(f"Attempt {job.attempt_count} succeeded")
    finally:
        db.close()


def reap_stale_jobs(db: Session, queue: JobQueue) -> int:
    cutoff = utcnow() - timedelta(seconds=get_settings().worker_stale_seconds)
    running = list(db.scalars(select(Job).where(Job.status == JobStatus.running.value)).all())
    reaped = 0
    for job in running:
        if _worker_is_fresh(db, job.worker_id, cutoff):
            continue
        _mark_failure(db, job, "Worker heartbeat expired before the job finished")
        reaped += 1
    if reaped:
        dispatch_queued(db, queue)
    return reaped


def require_job(db: Session, job_id: uuid.UUID) -> Job:
    job = db.get(Job, job_id)
    if job is None:
        raise JobError("Job not found", 404)
    return job


def list_logs(db: Session, job_id: uuid.UUID) -> list[JobLog]:
    require_job(db, job_id)
    return list(db.scalars(select(JobLog).where(JobLog.job_id == job_id).order_by(JobLog.id)).all())


def add_log(
    db: Session,
    *,
    job_id: uuid.UUID,
    message: str,
    level: str = "info",
    worker_id: uuid.UUID | None = None,
    attempt: int = 0,
) -> None:
    if level not in {"info", "warning", "error"}:
        level = "info"
    db.add(
        JobLog(
            job_id=job_id,
            worker_id=worker_id,
            attempt=attempt,
            level=level,
            message=message[:4000],
            created_at=utcnow(),
        )
    )


def _finish_failure(job_id: uuid.UUID, worker_id: uuid.UUID, exc: Exception) -> None:
    from app.database import SessionLocal

    db = SessionLocal()
    try:
        job = db.get(Job, job_id)
        if job is None or job.status != JobStatus.running.value:
            return
        message = str(exc).strip() or exc.__class__.__name__
        logger.info("job %s attempt failed: %s", job_id, message)
        _mark_failure(db, job, message, worker_id=worker_id)
        if job.status == JobStatus.queued.value:
            dispatch_queued(db, get_queue())
    finally:
        db.close()


def _mark_failure(db: Session, job: Job, message: str, worker_id: uuid.UUID | None = None) -> None:
    job.last_error = message[:2000]
    add_log(
        db,
        job_id=job.id,
        message=f"Attempt {job.attempt_count} failed: {job.last_error}",
        level="error",
        worker_id=worker_id or job.worker_id,
        attempt=job.attempt_count,
    )
    now = utcnow()
    job.worker_id = None
    job.updated_at = now
    if job.attempt_count <= job.max_retries:
        delay = get_settings().retry_backoff_seconds * (2 ** max(job.attempt_count - 1, 0))
        job.started_at = None
        job.dispatched = False
        if delay <= 0:
            job.status = JobStatus.queued.value
            job.run_at = now
            add_log(db, job_id=job.id, message="Queued for retry", attempt=job.attempt_count)
        else:
            job.status = JobStatus.scheduled.value
            job.run_at = now + timedelta(seconds=delay)
            add_log(
                db,
                job_id=job.id,
                message=f"Scheduled retry in {delay:g} seconds",
                attempt=job.attempt_count,
            )
    else:
        job.status = JobStatus.failed.value
        job.finished_at = now
        job.dispatched = False
        add_log(db, job_id=job.id, message="Retries exhausted", level="error", attempt=job.attempt_count)
    db.commit()


def _worker_is_fresh(db: Session, worker_id: uuid.UUID | None, cutoff) -> bool:
    if worker_id is None:
        return False
    worker = db.get(Worker, worker_id)
    if worker is None or worker.stopped_at is not None:
        return False
    return as_utc(worker.last_heartbeat_at) >= cutoff
