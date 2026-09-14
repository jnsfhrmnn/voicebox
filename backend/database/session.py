"""Engine creation, initialization, and session management."""

import logging

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from pathlib import Path

from .. import config
from ..schema import run_schema_upgrade, SchemaLock
from .models import Base  # noqa: F401 -- Re-Export (ORM-Metadaten)
from .migrations import run_migrations

logger = logging.getLogger(__name__)

# Initialized by init_db()
engine = None
SessionLocal = None
_db_path = None
# JFW-1 (Spec): Der laufende Server haelt eine gemeinsame OS-Schemalock fuer
# seine gesamte DB-Lebensdauer. Die Migration benoetigt exklusiven Zugriff —
# sie laeuft VOR dem Serverstart (Tauri) und ist dann kurz; der Server nimmt
# den shared Lock erst NACH erfolgreicher Initialisierung, damit er die eigene
# (No-Op-)Migration nicht blockiert.
_schema_lock = None


def init_db() -> None:
    """Bringt die DB auf ``alembic upgrade head`` und setzt das Engine-Setup.

    JFW-1: Die kanonische Schema-Linie sind die Alembic-Revisionen in
    ``backend/alembic/versions`` (nur ``captures`` + ``capture_settings``).
    Vor dem Upgrade werden Legacy-TTS-/LLM-Tabellen gedroppt und die
    idempotenten Column-Migrationen laufen, damit jede vorhandene Voicebox-DB
    auf das Kontrakt konvergiert. Das Seeding von TTS-/LLM-Daten ist entfernt.

    In Produktion hat Tauri die Migration VOR dem Serverstart ausgefuehrt;
    dieser Aufruf ist dann ein No-Op (bereits auf Head) und verifiziert nur.
    """
    global engine, SessionLocal, _db_path, _schema_lock

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

    # 3) JFW-1 (Spec): shared OS-Schemalock fuer die Server-Lebensdauer.
    _schema_lock = SchemaLock(_db_path, exclusive=False)
    _schema_lock.__enter__()


def get_db():
    """Yield a database session (FastAPI dependency)."""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
