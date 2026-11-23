import os
import tempfile

_TMP = tempfile.mkdtemp(prefix="taskflow-test-")
os.environ["DATABASE_URL"] = "sqlite:///" + os.path.join(_TMP, "test.db")
os.environ["REDIS_URL"] = "redis://127.0.0.1:6399/0"
os.environ["RETRY_BACKOFF_SECONDS"] = "0"
os.environ["WORKER_STALE_SECONDS"] = "15"

import fakeredis
import pytest
from fastapi.testclient import TestClient

import app.database as database
from app.main import app
from app.models import Base
from app.services.queue import set_redis


@pytest.fixture
def client():
    redis = fakeredis.FakeRedis(decode_responses=True)
    set_redis(redis)
    database.init_engine()
    Base.metadata.drop_all(bind=database.engine)
    Base.metadata.create_all(bind=database.engine)
    with TestClient(app) as test_client:
        yield test_client
    redis.flushall()
