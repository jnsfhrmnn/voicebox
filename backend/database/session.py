"""Engine creation, initialization, and session management."""

import logging

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from pathlib import Path

from .. import config
from ..schema import run_schema_upgrade
from .models import Base  # noqa: F401 -- Re-Export (ORM-Metadaten)
from .migrations import run_migrations

logger = logging.getLogger(__name__)

# Initialized by init_db()
engine = None
SessionLocal = None
_db_path = None


def init_db() -> None:
    """Bringt die DB auf ``alembic upgrade head`` und setzt das Engine-Setup.

    JFW-1: Die kanonische Schema-Linie sind die Alembic-Revisionen in
    ``backend/alembic/versions`` (nur ``captures`` + ``capture_settings``).
    Vor dem Upgrade werden Legacy-TTS-/LLM-Tabellen gedroppt und die
    idempotenten Column-Migrationen laufen, damit jede vorhandene Voicebox-DB
    auf das Kontrakt konvergiert. Das Seeding von TTS-/LLM-Daten ist entfernt.
    """
    global engine, SessionLocal, _db_path

    _db_path = config.get_db_path()
    _db_path.parent.mkdir(parents=True, exist_ok=True)

    engine = create_engine(
        f"sqlite:///{_db_path}",
        connect_args={"check_same_thread": False},
    )

    SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

    # 1) Safety Net: Legacy-TTS-/LLM-Tabellen drop + idempotente Column-Migrationen.
    run_migrations(engine)

    # 2) Kanonische Schema-Linie: alembic upgrade head (nur captures + capture_settings).
    run_schema_upgrade(_db_path, engine)


def get_db():
    """Yield a database session (FastAPI dependency)."""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
