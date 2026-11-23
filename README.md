# Taskflow API

Background job platform. Clients submit named jobs over HTTP, a worker runs them from Redis priority queues, and PostgreSQL keeps status, retries, schedules, heartbeats, and execution logs.

## What it does

- Submit jobs through a REST API
- Schedule a job for a future time
- Run high, normal, and low priority queues
- Track status from `scheduled` and `queued` through `running`, `succeeded`, `failed`, or `cancelled`
- Retry failed attempts with backoff, and retry a finished failure manually
- Register workers and record a heartbeat
- Recover a job when its worker stops heartbeating
- Store execution logs for every attempt
- Cancel a job that has not started

Jobs are type names plus a JSON payload. The worker runs a handler registered in the server. The API does not accept shell commands or arbitrary code.

## Architecture

```mermaid
flowchart LR
  Client --> API[FastAPI]
  API --> PG[(PostgreSQL)]
  API --> Redis
  Redis --> Worker[Taskflow worker]
  Worker --> PG
  Worker --> Redis
```

PostgreSQL is the source of truth for the job, its attempts, its logs, and the worker registry. Redis holds one FIFO list per priority: `taskflow:queue:high`, `taskflow:queue:normal`, and `taskflow:queue:low`. The worker checks those lists in that order.

On startup the worker inserts a row and then updates `last_heartbeat_at` on every poll. A worker is online while that heartbeat is newer than `WORKER_STALE_SECONDS` and the process has not stopped. If a job is still `running` after the worker goes stale, the worker loop queues it again when retries remain.

## Stack

FastAPI, PostgreSQL, Redis, SQLAlchemy, Alembic, and a custom worker process.

## Quick start

```bash
docker compose up --build
```

The API listens on http://localhost:8000. Interactive docs are at http://localhost:8000/docs. Compose runs migrations before the API serves traffic, and the worker starts after the API is healthy.

Submit a job:

```bash
curl -s -X POST http://localhost:8000/api/v1/jobs \
  -H 'content-type: application/json' \
  -d '{"type":"math.add","payload":{"numbers":[2,3,5]},"priority":"high"}'
```

The response is `202` with an `id`. Poll the job:

```bash
curl -s http://localhost:8000/api/v1/jobs/<id>
```

Read logs:

```bash
curl -s http://localhost:8000/api/v1/jobs/<id>/logs
```

Schedule work for later:

```bash
curl -s -X POST http://localhost:8000/api/v1/jobs \
  -H 'content-type: application/json' \
  -d '{"type":"text.echo","payload":{"text":"later"},"run_at":"2026-12-01T15:00:00Z"}'
```

That job stays `scheduled` until `run_at`. The worker then moves it onto the priority queue.

Cancel a job that is still `scheduled` or `queued`:

```bash
curl -s -X POST http://localhost:8000/api/v1/jobs/<id>/cancel
```

Retry a `failed` or `cancelled` job. This resets the attempt counter and queues it now:

```bash
curl -s -X POST http://localhost:8000/api/v1/jobs/<id>/retry
```

## Local development

PostgreSQL and Redis need to be running. Copy the environment file, migrate, then start the API and the worker in two shells.

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
alembic upgrade head
uvicorn app.main:app --reload
```

```bash
source .venv/bin/activate
python -m app.worker
```

## API

| Method | Path | Purpose |
| --- | --- | --- |
| `GET` | `/health` | Process is up |
| `GET` | `/ready` | Database and Redis are reachable |
| `GET` | `/api/v1/jobs/types` | Registered job types |
| `POST` | `/api/v1/jobs` | Submit a job |
| `GET` | `/api/v1/jobs` | List jobs. Filters: `status`, `priority`, `type`, `limit`, `offset` |
| `GET` | `/api/v1/jobs/{id}` | One job, including `result` or `last_error` |
| `GET` | `/api/v1/jobs/{id}/logs` | Execution logs in write order |
| `POST` | `/api/v1/jobs/{id}/cancel` | Cancel a scheduled or queued job |
| `POST` | `/api/v1/jobs/{id}/retry` | Run a failed or cancelled job again |
| `GET` | `/api/v1/workers` | Registered workers and whether each heartbeat is fresh |

### Submit body

| Field | Default | Meaning |
| --- | --- | --- |
| `type` | required | Registered handler name |
| `payload` | `{}` | JSON object checked for that type. Maximum 64 KB |
| `priority` | `normal` | `high`, `normal`, or `low` |
| `run_at` | now | ISO timestamp. A future time waits in `scheduled` |
| `max_retries` | `3` | Extra attempts after the first failure. `0` through `10` |

`max_retries: 3` means one try plus up to three retries, four runs in total. Backoff is `RETRY_BACKOFF_SECONDS * 2^(attempt - 1)`. The default base is 2 seconds, so the waits are 2s, 4s, then 8s. A base of `0` queues the retry immediately.

### Status

| status | Meaning |
| --- | --- |
| `scheduled` | Waiting for `run_at` |
| `queued` | Sitting on a Redis priority list |
| `running` | Claimed by a worker |
| `succeeded` | `result` holds the handler output |
| `failed` | Retries are used up. `last_error` has the reason |
| `cancelled` | Stopped before it started |

Cancellation of a `running` job returns `409`. Pending cancellation removes the id from Redis and, if a worker has already popped it, the claim update does not match a `queued` row so the handler does not run.

### Built-in job types

| Type | Payload | Result |
| --- | --- | --- |
| `math.add` | `{"numbers": [1, 2, 3]}` | `{"total": 6}` |
| `text.echo` | `{"text": "hello"}` | `{"text": "hello"}` |
| `demo.fail` | `{"message": "nope"}` | Fails so retry behavior can be seen |
| `demo.sleep` | `{"seconds": 1}` | Sleeps at most 30 seconds, then returns `{"slept_seconds": 1}` |

Add a new type by registering a handler in `app/services/handlers.py`.

### Workers

The worker process registers itself. It is not a public signup endpoint. `GET /api/v1/workers` reports `online: true` when `last_heartbeat_at` is within `WORKER_STALE_SECONDS` and the process has not recorded a stop. The default stale window is 15 seconds. The worker polls Redis about once a second.

A `running` job whose worker is stale is treated like a failed attempt: it is queued again when retries remain, otherwise it is marked `failed`.

### Logs

Each log row has `attempt`, `level` (`info`, `warning`, or `error`), and `message`. Handlers write through `ctx.log(...)`. The platform also records accept, start, success, failure, retry, schedule, and cancel events.

## Configuration

| Variable | Default | Purpose |
| --- | --- | --- |
| `DATABASE_URL` | `postgresql+psycopg://taskflow:taskflow@localhost:5432/taskflow` | SQLAlchemy URL |
| `REDIS_URL` | `redis://localhost:6379/0` | Priority queues |
| `RETRY_BACKOFF_SECONDS` | `2` | Base delay before an automatic retry |
| `WORKER_STALE_SECONDS` | `15` | Heartbeat age after which a worker is offline |
| `WORKER_POLL_SECONDS` | `1` | How long a worker waits for the next job |
| `WORKER_NAME` | hostname | Name stored at registration |
| `MAX_PAYLOAD_BYTES` | `65536` | Maximum JSON payload size |

## Tests

```bash
pytest
```

The tests use SQLite and an in-memory Redis stand-in, so PostgreSQL and Redis do not need to be running. Automatic retry backoff is set to `0` in the test environment so a retry is queued immediately.

## Project layout

```text
app/
  main.py                  FastAPI app
  worker.py                Registration, heartbeat, claim loop
  api/routes/              Jobs, workers, health
  services/handlers.py     Job type registry
  services/queue.py        Redis priority lists
  services/jobs.py         Submit, cancel, retry, execute, recover
alembic/                   PostgreSQL schema
docker-compose.yml         API, worker, PostgreSQL, Redis
tests/                     API and worker behavior
```

## Limitations

- Version 0.1 has no authentication. Put it behind a gateway before exposing it.
- Only handlers registered in the server can run. There is no plugin upload.
- A running handler runs to completion. Cancel applies to jobs that have not started.
- Retry backoff and stale recovery are handled by the worker loop. A queued job stays in Redis until a worker claims it.
- The local Compose password is for development.
