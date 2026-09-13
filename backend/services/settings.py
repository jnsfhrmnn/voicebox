"""
Server-side user settings — singleton rows persisted in SQLite so every
client window, API consumer, and headless flow reads the same preferences.

JFW-1 (Transkriptions-Produktprofil): Nur die Capture-Einstellungen sind
live; die Long-form-Generation-Domäne ist mit der TTS-Funktionalität
entfernt worden.
"""

from typing import Any

from sqlalchemy.orm import Session

from ..database import CaptureSettings as DBCaptureSettings
from ..utils.capture_chords import (
    default_push_to_talk_chord,
    default_toggle_to_talk_chord,
)


SINGLETON_ID = 1


def _get_or_create_capture_row(db: Session) -> DBCaptureSettings:
    row = db.query(DBCaptureSettings).filter(DBCaptureSettings.id == SINGLETON_ID).first()
    if row is None:
        row = DBCaptureSettings(
            id=SINGLETON_ID,
            chord_push_to_talk_keys=default_push_to_talk_chord(),
            chord_toggle_to_talk_keys=default_toggle_to_talk_chord(),
        )
        db.add(row)
        db.commit()
        db.refresh(row)
    return row




def update_capture_settings(db: Session, patch: dict[str, Any]) -> DBCaptureSettings:
    row = _get_or_create_capture_row(db)
    _apply_patch(row, patch)
    db.commit()
    db.refresh(row)
    return row

