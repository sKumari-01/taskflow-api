import uuid

import app.database as database
from app.services.jobs import execute_job
from app.services.queue import get_queue
from app.services.workers import register_worker


def run_next() -> str:
    job_id = get_queue().pop(0)
    assert job_id, "expected a queued job"
    db = database.SessionLocal()
    try:
        worker = register_worker(db, "test-worker")
        worker_id = worker.id
    finally:
        db.close()
    execute_job(uuid.UUID(job_id), worker_id)
    return job_id
