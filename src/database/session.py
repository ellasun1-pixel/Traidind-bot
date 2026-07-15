from __future__ import annotations

import logging
import os
from contextlib import contextmanager
from pathlib import Path

from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker, Session

from src.database.models import Base

logger = logging.getLogger(__name__)

_engine = None
_SessionLocal = None


def _is_production() -> bool:
    return os.environ.get("APP_ENV", "development").lower() == "production"


def _get_database_url() -> str:
    url = os.environ.get("DATABASE_URL", "")

    if _is_production():
        if not url:
            raise RuntimeError(
                "DATABASE_URL is required in production (APP_ENV=production). "
                "Set a PostgreSQL connection string."
            )
        normalized = url.replace("postgres://", "postgresql://", 1) if url.startswith("postgres://") else url
        if normalized.startswith("sqlite"):
            raise RuntimeError(
                "SQLite is not supported in production (APP_ENV=production). "
                "Set DATABASE_URL to a PostgreSQL connection string."
            )

    if not url:
        base_dir = Path(__file__).resolve().parent.parent.parent
        url = f"sqlite:///{base_dir / 'data' / 'challenge.db'}"
    if url.startswith("postgres://"):
        url = url.replace("postgres://", "postgresql://", 1)
    return url


def get_engine(database_url: str | None = None):
    global _engine
    if _engine is not None and database_url is None:
        return _engine

    url = database_url or _get_database_url()

    is_sqlite = url.startswith("sqlite")
    if is_sqlite:
        db_path = url.replace("sqlite:///", "")
        if db_path:
            Path(db_path).parent.mkdir(parents=True, exist_ok=True)

    kwargs = {"echo": False, "pool_pre_ping": True}
    if not is_sqlite:
        kwargs.update({
            "pool_size": 5,
            "max_overflow": 10,
            "pool_timeout": 30,
            "pool_recycle": 1800,
        })

    engine = create_engine(url, **kwargs)

    if database_url is None:
        _engine = engine
        if is_sqlite:
            logger.warning("Using SQLite — not for production")
        else:
            logger.info("Database engine created (PostgreSQL)")

    return engine


def _get_session_factory(engine=None) -> sessionmaker:
    global _SessionLocal
    if _SessionLocal is not None and engine is None:
        return _SessionLocal

    eng = engine or get_engine()
    factory = sessionmaker(bind=eng, expire_on_commit=False)

    if engine is None:
        _SessionLocal = factory

    return factory


@contextmanager
def get_session(engine=None):
    factory = _get_session_factory(engine)
    session: Session = factory()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


REQUIRED_TABLES = [
    "assets", "price_history", "signals", "paper_account",
    "paper_positions", "trade_history", "app_settings", "alert_history",
    "audit_log", "scheduler_state", "market_data_meta", "daily_snapshots",
    "health_transitions",
]


def init_db(engine=None):
    eng = engine or get_engine()
    url = str(eng.url)
    is_sqlite = url.startswith("sqlite")

    if is_sqlite:
        Base.metadata.create_all(eng)
        logger.info("SQLite tables created via metadata (dev mode)")
    else:
        missing = _check_required_tables(eng)
        if missing:
            logger.error(
                "PostgreSQL is missing required tables: %s. "
                "Run 'alembic upgrade head' before starting the application.",
                ", ".join(missing),
            )
            raise RuntimeError(
                f"Database schema incomplete — missing tables: {', '.join(missing)}. "
                f"Run 'alembic upgrade head' first."
            )
        logger.info("PostgreSQL schema verified — all %d required tables present", len(REQUIRED_TABLES))


def _check_required_tables(engine) -> list[str]:
    from sqlalchemy import inspect
    inspector = inspect(engine)
    existing = set(inspector.get_table_names())
    return [t for t in REQUIRED_TABLES if t not in existing]


def check_db_health(engine=None) -> dict:
    eng = engine or get_engine()
    try:
        with eng.connect() as conn:
            conn.execute(text("SELECT 1"))
        return {"status": "ok", "backend": eng.dialect.name}
    except Exception as e:
        return {"status": "error", "error": str(e)}


def reset_engine():
    global _engine, _SessionLocal
    if _engine is not None:
        _engine.dispose()
    _engine = None
    _SessionLocal = None
