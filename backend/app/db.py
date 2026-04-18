"""SQLAlchemy engine, session factory, and declarative base."""

from __future__ import annotations

import sys
from collections.abc import Generator

from sqlalchemy import create_engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from app.config import get_settings

_settings = get_settings()

engine = create_engine(_settings.database_url, pool_pre_ping=True, future=True)
SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False, future=True)


class Base(DeclarativeBase):
    """Project-wide declarative base."""


def get_db() -> Generator[Session, None, None]:
    """FastAPI dependency that yields a SQLAlchemy session."""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def init_db() -> None:
    """Create all tables. For Phase 1 we skip Alembic migrations."""
    # Import models so they register on Base.metadata
    from app import models  # noqa: F401

    Base.metadata.create_all(bind=engine)
    print("Database initialized.")


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "init":
        init_db()
    else:
        print("Usage: python -m app.db init")
