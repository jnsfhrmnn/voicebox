"""Engine creation, initialization, and session management."""

import logging

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from .. import config
from .models import Base
from .migrations import run_migrations

logger = logging.getLogger(__name__)

# Initialized by init_db()
engine = None
SessionLocal = None
_db_path = None


def init_db() -> None:
    """Initialize the database engine, run migrations, and create tables.

    JFW-1: Es werden nur die Tabellen des Transkriptionsprofils angelegt
    (``captures``, ``capture_settings``). Das Seeding von TTS-/LLM-Daten
    (Audio-Kanäle, Effekt-Presets, Generation-Versionen) ist entfernt --
    es gab dazu im aktiven Profil weder Routen noch Tabellen.
    """
    global engine, SessionLocal, _db_path

    _db_path = config.get_db_path()
    _db_path.parent.mkdir(parents=True, exist_ok=True)

    engine = create_engine(
        f"sqlite:///{_db_path}",
        connect_args={"check_same_thread": False},
    )

    SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

    run_migrations(engine)
    Base.metadata.create_all(bind=engine)


def get_db():
    """Yield a database session (FastAPI dependency)."""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
