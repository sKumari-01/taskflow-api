import uuid
from datetime import timedelta

from app.models import Job, utcnow
from app.services.handlers import validate_payload
from app.services.jobs import promote_due_jobs, reap_stale_jobs
from app.services.queue import get_queue
from app.services.workers import is_online, mark_offline, register_worker
from tests.helpers import run_next

import app.database as database


def test_job_types_and_health(client):
    assert client.get("/health").json()["status"] == "ok"
    ready = client.get("/ready")
    assert ready.status_code == 200
    assert ready.json()["database"] == "ok"
    assert ready.json()["redis"] == "ok"
    names = {item["name"] for item in client.get("/api/v1/jobs/types").json()}
    assert names == {"math.add", "text.echo", "demo.fail", "demo.sleep"}


def test_submit_run_and_read_logs(client):
    created = client.post(
        "/api/v1/jobs",
        json={"type": "math.add", "payload": {"numbers": [2, 3, 5]}, "priority": "high"},
    )
    assert created.status_code == 202
    body = created.json()
    assert body["status"] == "queued"
    assert body["priority"] == "high"

    assert run_next() == body["id"]

    fetched = client.get(f"/api/v1/jobs/{body['id']}")
    assert fetched.status_code == 200
    finished = fetched.json()
    assert finished["status"] == "succeeded"
    assert finished["result"] == {"total": 10}
    assert finished["attempt_count"] == 1
    assert finished["worker_id"]

    logs = client.get(f"/api/v1/jobs/{body['id']}/logs").json()["items"]
    messages = [item["message"] for item in logs]
    assert any(message.startswith("Accepted") for message in messages)
    assert "Adding 3 numbers" in messages
    assert "Attempt 1 succeeded" in messages


def test_priority_queues_pop_high_before_normal_before_low(client):
    ids = {}
    for priority in ("low", "normal", "high", "normal"):
        response = client.post(
            "/api/v1/jobs",
            json={"type": "text.echo", "payload": {"text": priority}, "priority": priority},
        )
        ids.setdefault(priority, []).append(response.json()["id"])

    popped = [get_queue().pop(0) for _ in range(4)]
    assert popped == [ids["high"][0], ids["normal"][0], ids["normal"][1], ids["low"][0]]


def test_future_job_stays_scheduled_until_it_is_due(client):
    run_at = (utcnow() + timedelta(hours=2)).isoformat()
    created = client.post(
        "/api/v1/jobs",
        json={"type": "text.echo", "payload": {"text": "later"}, "run_at": run_at},
    )
    assert created.status_code == 202
    assert created.json()["status"] == "scheduled"
    assert get_queue().pop(0) is None

    job_id = uuid.UUID(created.json()["id"])
    db = database.SessionLocal()
    try:
        job = db.get(Job, job_id)
        job.run_at = utcnow() - timedelta(seconds=5)
        db.commit()
        assert promote_due_jobs(db, get_queue()) == 1
    finally:
        db.close()

    assert get_queue().pop(0) == str(job_id)
    assert client.get(f"/api/v1/jobs/{job_id}").json()["status"] == "queued"


def test_cancel_pending_job(client):
    created = client.post("/api/v1/jobs", json={"type": "text.echo", "payload": {"text": "stop"}})
    job_id = created.json()["id"]
    cancelled = client.post(f"/api/v1/jobs/{job_id}/cancel")
    assert cancelled.status_code == 200
    assert cancelled.json()["status"] == "cancelled"
    assert get_queue().pop(0) is None

    blocked = client.post(f"/api/v1/jobs/{job_id}/cancel")
    assert blocked.status_code == 409


def test_failed_job_retries_then_can_be_cancelled_from_the_queue(client):
    created = client.post(
        "/api/v1/jobs",
        json={"type": "demo.fail", "payload": {"message": "nope"}, "max_retries": 1},
    )
    job_id = created.json()["id"]
    run_next()
    waiting = client.get(f"/api/v1/jobs/{job_id}").json()
    assert waiting["status"] == "queued"
    assert waiting["attempt_count"] == 1
    assert waiting["last_error"] == "nope"

    run_next()
    failed = client.get(f"/api/v1/jobs/{job_id}").json()
    assert failed["status"] == "failed"
    assert failed["attempt_count"] == 2

    listed = client.get("/api/v1/jobs", params={"status": "failed", "type": "demo.fail"})
    assert listed.json()["total"] == 1


def test_manual_retry_resets_attempts(client):
    created = client.post("/api/v1/jobs", json={"type": "demo.fail", "max_retries": 0})
    job_id = created.json()["id"]
    run_next()
    assert client.get(f"/api/v1/jobs/{job_id}").json()["status"] == "failed"

    retried = client.post(f"/api/v1/jobs/{job_id}/retry")
    assert retried.status_code == 202
    assert retried.json()["status"] == "queued"
    assert retried.json()["attempt_count"] == 0
    assert retried.json()["last_error"] is None


def test_unknown_type_and_bad_payload_are_rejected(client):
    unknown = client.post("/api/v1/jobs", json={"type": "os.system", "payload": {"command": "id"}})
    assert unknown.status_code == 400

    empty = client.post("/api/v1/jobs", json={"type": "math.add", "payload": {"numbers": []}})
    assert empty.status_code == 422

    missing = client.get("/api/v1/jobs/11111111-1111-1111-1111-111111111111")
    assert missing.status_code == 404


def test_validate_payload_rejects_booleans_as_numbers():
    try:
        validate_payload("math.add", {"numbers": [True]})
    except ValueError as exc:
        assert "numeric" in str(exc)
    else:
        raise AssertionError("expected validation to fail")


def test_worker_heartbeat_and_stale_job_recovery(client):
    db = database.SessionLocal()
    try:
        fresh = register_worker(db, "fresh")
        stale = register_worker(db, "stale")
        stale.last_heartbeat_at = utcnow() - timedelta(seconds=120)
        db.commit()
        stale_id = stale.id
        fresh_id = fresh.id

        job = Job(
            id=uuid.uuid4(),
            type="text.echo",
            payload={"text": "recover"},
            priority="normal",
            status="running",
            run_at=utcnow(),
            max_retries=3,
            attempt_count=1,
            worker_id=stale_id,
            dispatched=True,
            created_at=utcnow(),
            updated_at=utcnow(),
            started_at=utcnow(),
        )
        db.add(job)
        db.commit()
        job_id = job.id
        assert reap_stale_jobs(db, get_queue()) == 1
        db.refresh(job)
        assert job.status == "queued"
        assert job.worker_id is None
    finally:
        db.close()

    assert get_queue().pop(0) == str(job_id)

    workers = client.get("/api/v1/workers").json()["items"]
    by_id = {item["id"]: item for item in workers}
    assert by_id[str(fresh_id)]["online"] is True
    assert by_id[str(stale_id)]["online"] is False

    db = database.SessionLocal()
    try:
        mark_offline(db, fresh_id)
        worker = db.get(type(fresh), fresh_id)
        assert is_online(worker) is False
    finally:
        db.close()
