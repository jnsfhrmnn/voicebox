"""ORM model definitions for the JF Whisper SQLite database.

JFW-1 (Transkriptions-Produktprofil): Die TTS-/LLM-Tabellen (profiles,
generations, stories, projects, effect_presets, audio_channels, ...) sind
aus dem Laufzeitvertrag entfernt. Das Schema-Kontrakt erlaubt exakt die
Tabellen ``captures`` und ``capture_settings`` -- alles andere darf nicht
materialisiert werden (Gate: scripts/verify_schema.py).
"""

from datetime import datetime
import uuid

from sqlalchemy import Column, String, Integer, Float, DateTime, Text, Boolean, JSON
from sqlalchemy.ext.declarative import declarative_base

from ..utils.capture_chords import (
    default_push_to_talk_chord,
    default_toggle_to_talk_chord,
)

Base = declarative_base()


class CaptureSettings(Base):
    """Singleton row holding user defaults for the capture flow.

    Kept server-side so every window, CLI client, and API consumer reads the
    same preferences. The ``id`` column is always 1.
    """

    __tablename__ = "capture_settings"

    id = Column(Integer, primary_key=True, default=1)
    stt_model = Column(String, nullable=False, default="turbo")
    language = Column(String, nullable=False, default="auto")
    allow_auto_paste = Column(Boolean, nullable=False, default=True)
    # Default OFF -- opting in is what triggers the macOS Input Monitoring TCC
    # prompt. We deliberately don't spawn the global keyboard tap until the
    # user flips this on so a fresh-install user doesn't see a scary
    # "Voicebox would like to receive keystrokes from any application" dialog
    # before they've even opened the Captures tab.
    hotkey_enabled = Column(Boolean, nullable=False, default=False)
    # Lists of keytap key names (e.g. "MetaRight", "ControlRight"). Right-hand
    # modifiers by default so they don't collide with left-hand shortcuts.
    chord_push_to_talk_keys = Column(
        JSON, nullable=False, default=default_push_to_talk_chord
    )
    chord_toggle_to_talk_keys = Column(
        JSON, nullable=False, default=default_toggle_to_talk_chord
    )
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class Capture(Base):
    """A single voice input capture (dictation, recording, or uploaded file).

    Stores the original audio alongside the raw transcript. JFW-1: das
    Transkript ist Endzustand — Refinement-/LLM-Spalten sind aus dem
    Schema-Kontrakt entfernt (Spec: LLM-/TTS-Felder sind kein Zielpfad).
    """

    __tablename__ = "captures"

    id = Column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    audio_path = Column(String, nullable=False)
    source = Column(String, nullable=False, default="file")  # dictation | recording | file
    language = Column(String, nullable=True)
    duration_ms = Column(Integer, nullable=True)
    transcript_raw = Column(Text, nullable=False, default="")
    stt_model = Column(String, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)
