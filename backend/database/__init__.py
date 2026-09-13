"""Database package -- ORM models, session management, and migrations.

JFW-1 (Transkriptions-Produktprofil): Re-Export der Live-Symbole. Die
TTS-/LLM-Modelle sind aus dem Laufzeitvertrag entfernt; das Schema-Kontrakt
erlaubt exakt die Tabellen ``captures`` und ``capture_settings``.
"""

from .models import Base, Capture, CaptureSettings
from .session import engine, SessionLocal, _db_path, init_db, get_db

__all__ = [
    # Models
    "Base",
    "Capture",
    "CaptureSettings",
    # Session
    "engine",
    "SessionLocal",
    "_db_path",
    "init_db",
    "get_db",
]
