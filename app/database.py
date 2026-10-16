"""SQLAlchemy engine and session management.

Uses local SQLite today. To move to AWS (RDS Postgres) later, only
DATABASE_URL in .env needs to change - no code changes required.
"""
from __future__ import annotations

from sqlalchemy import create_engine
from sqlalchemy.orm import DeclarativeBase, sessionmaker

from app.config import settings

_connect_args = {}
if settings.database_url.startswith("sqlite"):
    # allow use across FastAPI's threadpool / background hardware threads
    _connect_args = {"check_same_thread": False}

engine = create_engine(
    settings.database_url,
    connect_args=_connect_args,
    future=True,
)

SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False, future=True)


class Base(DeclarativeBase):
    pass


def get_db():
    """FastAPI dependency: yields a session and always closes it."""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def init_db() -> None:
    """Create tables if they do not exist."""
    from app import models  # noqa: F401  (register models on Base)

    Base.metadata.create_all(bind=engine)
