"""Taskflow worker loop."""

from __future__ import annotations

import logging
import signal
import uuid

import app.database as database
from app.config import get_settings
from app.services.jobs import dispatch_queued, execute_job, promote_due_jobs, reap_stale_jobs
from app.services.queue import get_queue
from app.services.workers import heartbeat, mark_offline, register_worker

logger = logging.getLogger(__name__)


def run_forever() -> None:
    database.init_engine()
    db = database.SessionLocal()
    try:
        worker = register_worker(db, get_settings().worker_name or None)
        worker_id = worker.id
    finally:
        db.close()
    logger.info("worker %s registered", worker_id)

    stop = False

    def request_stop(signum, _frame) -> None:
        nonlocal stop
        logger.info("worker %s received signal %s", worker_id, signum)
        stop = True

    signal.signal(signal.SIGINT, request_stop)
    signal.signal(signal.SIGTERM, request_stop)

    while not stop:
        db = database.SessionLocal()
        try:
            heartbeat(db, worker_id)
            queue = get_queue()
            promote_due_jobs(db, queue)
            dispatch_queued(db, queue)
            reap_stale_jobs(db, queue)
        except Exception:
            logger.exception("worker bookkeeping failed")
        finally:
            db.close()

        try:
            job_id = get_queue().pop(timeout=get_settings().worker_poll_seconds)
        except Exception:
            logger.exception("queue pop failed")
            continue
        if not job_id:
            continue
        try:
            execute_job(uuid.UUID(job_id), worker_id)
        except Exception:
            logger.exception("job %s crashed the worker loop", job_id)

    db = database.SessionLocal()
    try:
        mark_offline(db, worker_id)
    finally:
        db.close()
    logger.info("worker %s stopped", worker_id)


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    run_forever()


if __name__ == "__main__":
    main()
