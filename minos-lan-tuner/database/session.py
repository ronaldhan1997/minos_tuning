"""Engine lifecycle. Tests replace the URL before creating the app."""

from __future__ import annotations

from pathlib import Path

from sqlalchemy import create_engine, event, inspect, text
from sqlalchemy.engine import Connection, Engine
from sqlalchemy.orm import Session, sessionmaker

from database.models import Base

_engine: Engine | None = None
_database_url: str | None = None
SessionLocal: sessionmaker[Session] | None = None


def normalize_database_url(database_url: str) -> str:
    """Make a relative sqlite path absolute so Optuna and SQLAlchemy share it."""
    prefix = "sqlite:///"
    if not database_url.startswith(prefix) or database_url == "sqlite://":
        return database_url
    raw = database_url[len(prefix):]
    if raw == ":memory:" or raw.startswith("file:"):
        return database_url
    path = Path(raw)
    if not path.is_absolute():
        path = Path.cwd() / path
    return prefix + path.resolve().as_posix()


def init_engine(database_url: str) -> Engine:
    global _engine, _database_url, SessionLocal
    database_url = normalize_database_url(database_url)
    if _engine is not None:
        _engine.dispose()
    connect_args = {}
    if database_url.startswith("sqlite"):
        connect_args["check_same_thread"] = False
        connect_args["timeout"] = 30
    engine = create_engine(database_url, connect_args=connect_args, future=True)
    if database_url.startswith("sqlite"):

        @event.listens_for(engine, "connect")
        def _sqlite_pragmas(dbapi_connection, _connection_record) -> None:  # type: ignore[no-untyped-def]
            cursor = dbapi_connection.cursor()
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.execute("PRAGMA journal_mode=WAL")
            cursor.close()

    Base.metadata.create_all(engine)
    with engine.begin() as conn:
        ensure_dashboard_columns(conn)
    _engine = engine
    _database_url = database_url
    SessionLocal = sessionmaker(bind=engine, expire_on_commit=False, future=True)
    return engine


def ensure_dashboard_columns(bind: Connection) -> None:
    """Add columns introduced after the first create_all.

    ``create_all`` does not alter an existing table. Fresh databases already
    have the columns, so each statement is skipped when the column exists.
    """
    inspector = inspect(bind)
    tables = set(inspector.get_table_names())
    statements: list[str] = []
    if "workers" in tables:
        have = {column["name"] for column in inspector.get_columns("workers")}
        if "scheduling_mode" not in have:
            statements.append(
                "ALTER TABLE workers ADD COLUMN scheduling_mode VARCHAR(16) NOT NULL DEFAULT 'enabled'"
            )
        if "policy_max_parallel" not in have:
            statements.append("ALTER TABLE workers ADD COLUMN policy_max_parallel INTEGER")
        if "ram_used_mb" not in have:
            statements.append("ALTER TABLE workers ADD COLUMN ram_used_mb INTEGER")
        if "disk_total_mb" not in have:
            statements.append("ALTER TABLE workers ADD COLUMN disk_total_mb INTEGER")
    if "jobs" in tables:
        have = {column["name"] for column in inspector.get_columns("jobs")}
        if "log_excerpt" not in have:
            statements.append("ALTER TABLE jobs ADD COLUMN log_excerpt TEXT")
    for statement in statements:
        bind.execute(text(statement))


def current_database_url() -> str:
    if _database_url is None:
        raise RuntimeError("Database engine is not initialized")
    return _database_url


def get_engine() -> Engine:
    if _engine is None:
        raise RuntimeError("Database engine is not initialized")
    return _engine


def session() -> Session:
    if SessionLocal is None:
        raise RuntimeError("Database engine is not initialized")
    return SessionLocal()
