"""Redis priority queues.

Each priority is a FIFO list. Workers check high, then normal, then low.
"""

from __future__ import annotations

from redis import Redis

from app.config import get_settings

PRIORITIES = ("high", "normal", "low")

_client: Redis | None = None


def queue_key(priority: str) -> str:
    return f"taskflow:queue:{priority}"


def get_redis() -> Redis:
    global _client
    if _client is None:
        _client = Redis.from_url(get_settings().redis_url, decode_responses=True)
    return _client


def set_redis(client: Redis | None) -> None:
    global _client
    _client = client


class JobQueue:
    def __init__(self, client: Redis):
        self.client = client

    def enqueue(self, job_id: str, priority: str) -> None:
        self.client.rpush(queue_key(priority), job_id)

    def remove(self, job_id: str, priority: str) -> None:
        self.client.lrem(queue_key(priority), 0, job_id)

    def pop(self, timeout: float = 0) -> str | None:
        for priority in PRIORITIES:
            value = self.client.lpop(queue_key(priority))
            if value is not None:
                return str(value)
        if timeout <= 0:
            return None
        popped = self.client.blpop([queue_key(priority) for priority in PRIORITIES], timeout=timeout)
        if not popped:
            return None
        return str(popped[1])


def get_queue() -> JobQueue:
    return JobQueue(get_redis())
