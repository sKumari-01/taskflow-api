import uuid

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.database import get_db
from app.models import Job, JobPriority, JobStatus
from app.schemas import JobCreate, JobList, JobTypeView, JobView, LogList, LogView
from app.services.handlers import known_types
from app.services.jobs import JobError, cancel_job, list_logs, require_job, retry_job, submit_job
from app.services.queue import get_queue

router = APIRouter(prefix="/jobs", tags=["jobs"])


@router.get("/types", response_model=list[JobTypeView])
def list_job_types():
    """List the job types this server knows how to run."""
    return [JobTypeView(name=item.name, description=item.description) for item in known_types()]


@router.post("", response_model=JobView, status_code=202)
def create_job(body: JobCreate, db: Session = Depends(get_db)):
    """Submit a job. Future `run_at` values stay scheduled until they are due."""
    try:
        return submit_job(
            db,
            get_queue(),
            job_type=body.type,
            payload=body.payload,
            priority=body.priority,
            run_at=body.run_at,
            max_retries=body.max_retries,
        )
    except JobError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.detail) from exc


@router.get("", response_model=JobList)
def list_jobs(
    status: JobStatus | None = None,
    priority: JobPriority | None = None,
    job_type: str | None = Query(default=None, alias="type", max_length=64),
    limit: int = Query(default=20, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
):
    """List jobs, newest first."""
    criteria = []
    if status is not None:
        criteria.append(Job.status == status.value)
    if priority is not None:
        criteria.append(Job.priority == priority.value)
    if job_type:
        criteria.append(Job.type == job_type)
    total = db.scalar(select(func.count()).select_from(Job).where(*criteria)) or 0
    rows = db.scalars(select(Job).where(*criteria).order_by(Job.created_at.desc()).limit(limit).offset(offset)).all()
    return JobList(
        items=[JobView.model_validate(row) for row in rows],
        total=total,
        limit=limit,
        offset=offset,
    )


@router.get("/{job_id}", response_model=JobView)
def get_job(job_id: uuid.UUID, db: Session = Depends(get_db)):
    """Return one job, including its result or last error."""
    try:
        return require_job(db, job_id)
    except JobError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.detail) from exc


@router.get("/{job_id}/logs", response_model=LogList)
def get_job_logs(job_id: uuid.UUID, db: Session = Depends(get_db)):
    """Return execution logs in the order they were written."""
    try:
        logs = list_logs(db, job_id)
    except JobError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.detail) from exc
    return LogList(items=[LogView.model_validate(log) for log in logs])


@router.post("/{job_id}/cancel", response_model=JobView)
def cancel(job_id: uuid.UUID, db: Session = Depends(get_db)):
    """Cancel a job that has not started running."""
    try:
        return cancel_job(db, get_queue(), job_id)
    except JobError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.detail) from exc


@router.post("/{job_id}/retry", response_model=JobView, status_code=202)
def retry(job_id: uuid.UUID, db: Session = Depends(get_db)):
    """Queue a failed or cancelled job again and reset its attempt counter."""
    try:
        return retry_job(db, get_queue(), job_id)
    except JobError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.detail) from exc
