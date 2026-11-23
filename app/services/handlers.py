"""Registered job types.

Clients submit a type name and a JSON payload. The worker runs the matching
handler. Executable code is not accepted from the API.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any


class JobContext:
    def log(self, message: str, level: str = "info") -> None:
        raise NotImplementedError


Handler = Callable[[dict[str, Any], JobContext], dict]


@dataclass(frozen=True)
class JobType:
    name: str
    description: str
    handler: Handler


def handle_add(payload: dict, ctx: JobContext) -> dict:
    numbers = payload["numbers"]
    ctx.log(f"Adding {len(numbers)} numbers")
    total = sum(numbers)
    return {"total": total}


def handle_echo(payload: dict, ctx: JobContext) -> dict:
    text = payload.get("text", "")
    ctx.log("Echoing text")
    return {"text": text}


def handle_fail(payload: dict, ctx: JobContext) -> dict:
    message = payload.get("message") or "demo failure"
    ctx.log(message, level="error")
    raise RuntimeError(message)


def handle_sleep(payload: dict, ctx: JobContext) -> dict:
    seconds = float(payload.get("seconds", 0))
    ctx.log(f"Sleeping for {seconds} seconds")
    time.sleep(seconds)
    return {"slept_seconds": seconds}


REGISTRY: dict[str, JobType] = {
    "math.add": JobType("math.add", "Add a list of numbers and store the total.", handle_add),
    "text.echo": JobType("text.echo", "Return the submitted text.", handle_echo),
    "demo.fail": JobType("demo.fail", "Fail on purpose so retries can be observed.", handle_fail),
    "demo.sleep": JobType("demo.sleep", "Sleep for a bounded number of seconds, then succeed.", handle_sleep),
}


def known_types() -> list[JobType]:
    return list(REGISTRY.values())


def get_job_type(name: str) -> JobType | None:
    return REGISTRY.get(name)


def validate_payload(job_type: str, payload: dict) -> None:
    if job_type == "math.add":
        numbers = payload.get("numbers")
        if not isinstance(numbers, list) or not 1 <= len(numbers) <= 1000:
            raise ValueError("numbers must be a list of 1 to 1000 numbers")
        if not all(isinstance(number, (int, float)) and not isinstance(number, bool) for number in numbers):
            raise ValueError("numbers must be numeric")
        return
    if job_type == "text.echo":
        text = payload.get("text", "")
        if not isinstance(text, str) or len(text) > 10000:
            raise ValueError("text must be a string of at most 10000 characters")
        return
    if job_type == "demo.fail":
        message = payload.get("message", "demo failure")
        if not isinstance(message, str) or len(message) > 500:
            raise ValueError("message must be a string of at most 500 characters")
        return
    if job_type == "demo.sleep":
        seconds = payload.get("seconds", 0)
        if isinstance(seconds, bool) or not isinstance(seconds, (int, float)) or not 0 <= float(seconds) <= 30:
            raise ValueError("seconds must be a number between 0 and 30")
        return
    raise ValueError(f"Unknown job type: {job_type}")
