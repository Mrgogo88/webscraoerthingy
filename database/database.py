"""Database engine, session management, and initialization for LeadFinder."""

from __future__ import annotations

from contextlib import contextmanager
from typing import Iterator

from sqlalchemy import create_engine, event, inspect, text
from sqlalchemy.orm import Session, sessionmaker

import config
from database.models import Base

_engine = None
_SessionFactory = None


def get_engine():
    """Create (once) and return the SQLAlchemy engine."""
    global _engine
    if _engine is None:
        config.ensure_dirs()
        _engine = create_engine(
            config.DATABASE_URL,
            # Streamlit and the async crawler may touch the DB from
            # different threads; SQLite needs this flag for that.
            connect_args={"check_same_thread": False},
        )

        # Enforce foreign keys in SQLite (off by default).
        @event.listens_for(_engine, "connect")
        def _fk_pragma(dbapi_conn, _record):
            cursor = dbapi_conn.cursor()
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.close()

    return _engine


# Columns added after the initial release; applied to existing DBs on startup.
_MIGRATIONS: dict[str, dict[str, str]] = {
    "businesses": {
        "website_source": "VARCHAR(20)",
        "website_status": "VARCHAR(20)",
        "is_chain": "BOOLEAN",
    },
    "website_analysis": {
        "phones_found": "TEXT",
        "copyright_year": "INTEGER",
        "legacy_signals": "TEXT",
        "is_outdated": "BOOLEAN",
    },
}

# One-off statements run after column additions (idempotent).
_POST_MIGRATION_SQL = [
    "UPDATE businesses SET is_chain = 0 WHERE is_chain IS NULL",
]


def _migrate(engine) -> None:
    """Add any missing columns to existing tables (SQLite-safe ALTERs)."""
    inspector = inspect(engine)
    for table, columns in _MIGRATIONS.items():
        if table not in inspector.get_table_names():
            continue
        existing = {c["name"] for c in inspector.get_columns(table)}
        for column, ddl_type in columns.items():
            if column not in existing:
                with engine.begin() as conn:
                    conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {column} {ddl_type}"))
    with engine.begin() as conn:
        for stmt in _POST_MIGRATION_SQL:
            conn.execute(text(stmt))


def init_db() -> None:
    """Create all tables if they do not exist, then apply column migrations."""
    engine = get_engine()
    Base.metadata.create_all(engine)
    _migrate(engine)


@contextmanager
def get_session() -> Iterator[Session]:
    """Provide a transactional session scope.

    Commits on success, rolls back on error, always closes.
    """
    global _SessionFactory
    if _SessionFactory is None:
        _SessionFactory = sessionmaker(bind=get_engine(), expire_on_commit=False)
    session = _SessionFactory()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()
