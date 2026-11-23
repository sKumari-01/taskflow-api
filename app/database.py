from collections.abc import Iterator

from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from app.config import get_settings

engine = None
SessionLocal = None
_engine_url: str | None = None


def init_engine(url: str | None = None):
    """Create the SQLAlchemy engine once per database URL."""
    global engine, SessionLocal, _engine_url
    database_url = url or get_settings().database_url
    if engine is not None and _engine_url == database_url:
        return engine

    connect_args = {"check_same_thread": False} if database_url.startswith("sqlite") else {}
    engine = create_engine(database_url, pool_pre_ping=True, connect_args=connect_args)
    SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    _engine_url = database_url
    return engine


def get_db() -> Iterator[Session]:
    init_engine()
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
