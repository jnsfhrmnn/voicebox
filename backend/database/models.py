"""ORM model definitions for the JF Whisper SQLite database.

JFW-1 (Transkriptions-Produktprofil): Die TTS-/LLM-Tabellen (profiles,
generations, stories, projects, effect_presets, audio_channels, ...) sind
aus dem Laufzeitvertrag entfernt. Das Schema-Kontrakt erlaubt exakt die
Tabellen ``captures`` und ``capture_settings`` -- alles andere darf nicht
materialisiert werden (Gate: scripts/verify_schema.py).
"""

from datetime import datetime
import uuid

from sqlalchemy import Column, String, Integer, DateTime, Text, Boolean, JSON
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


# ── JFW-12: Task-/Lease-Datenvertrag (Spec B7 + C) ───────────────────────
# ``tasks`` trägt den Attempt-Zustand (Job/Attempt/Epoche/Generation/Vertrag/
# Status/Cancel-/Terminalzeit). ``transcript_revisions`` hält das genau-einmal
# autoritative Rohresultat, gebunden an eine eindeutige ``source_attempt_id``.
# Beide Tabellen sind JFW-12-reserviert (JFW-1-Schema-Kontrakt erlaubt sie erst
# ab JFW-12) und bilden die Exactly-once-Basis für Drain/Cancel/Finalisierung.

#: Erlaubte Attempt-Statuswerte (Spec B7: nichtterminal vs. terminal).
TASK_STATUSES = ("pending", "running", "succeeded", "failed", "cancelled")
#: Terminaler Status = keine weitere Zustellung/Finalisierung mehr möglich.
TERMINAL_TASK_STATUSES = frozenset({"succeeded", "failed", "cancelled"})


class TaskAttempt(Base):
    """Ein einzelner, generationen- und epochengebundener Verarbeitungsattempt.

    JFW-12 (Spec B7/C): Jeder Attempt bindet ``job_id``, ``id`` (= attempt_id),
    ``app_epoch``, ``backend_generation``, den Backend-/Modellvertrag
    (``model_contract_hash``) und den Eingabehash (``input_hash``). Der Status
    ist nichtterminal (``pending``/``running``) oder terminal
    (``succeeded``/``failed``/``cancelled``). Eine terminale Transition wird nur
    bedingt auf den noch aktiven Attempt gesetzt (Exactly-once, siehe
    ``services.task_contract.finalize_attempt``).
    """

    __tablename__ = "tasks"

    id = Column(String, primary_key=True)  # attempt_id (uuid4, vom Supervisor vergeben)
    job_id = Column(String, nullable=False, index=True)  # logische Job-Identität (mehrere Attempts möglich)
    app_epoch = Column(String, nullable=False)  # zufällige App-Sitzungsepoche (Rust, RAM)
    backend_generation = Column(Integer, nullable=False)  # monotoner Generationszähler
    backend_variant = Column(String, nullable=False)  # cpu | cuda
    model_contract_hash = Column(String, nullable=False)  # Backend-/Modellvertrag (hashgebunden)
    input_hash = Column(String, nullable=False)  # Eingabesnapshot-Hash (Audio)
    status = Column(String, nullable=False, default="pending")  # TASK_STATUSES
    cancel_requested_at = Column(DateTime, nullable=True)
    terminal_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(
        DateTime, default=datetime.utcnow, onupdate=datetime.utcnow
    )


class TranscriptRevision(Base):
    """Das genau-einmal autoritative Rohresultat eines Attempts.

    JFW-12 (Spec B7/C): ``source_attempt_id`` ist eindeutig — ein Attempt kann
    höchstens eine autoritäre Rohrevision besitzen. Die Revision wird zusammen
    mit der terminalen Tasktransition in einer DB-Transaktion gespeichert; die
    eindeutige Referenz verhindert doppelte Rohrevisionen bei konkurrierender
    Finalisierung/Cancel.
    """

    __tablename__ = "transcript_revisions"

    id = Column(String, primary_key=True)  # uuid4
    source_attempt_id = Column(
        String, nullable=False, unique=True, index=True
    )  # FK -> tasks.id (eindeutig: genau einmal autoritativ)
    transcript_raw = Column(Text, nullable=False, default="")
    stt_model = Column(String, nullable=True)
    language = Column(String, nullable=True)
    duration_ms = Column(Integer, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)


# ── JFW-2: Alignment-Ergebnisvertrag (Spec „Alignment Result Contract") ────
# Eine Ergebnisebene je Identitaet (job + audio + Transkriptrevision + Profil +
# Vertragsversion). ``identity_hash`` ist UNIQUE — hoechstens ein autoritatives
# Ergebnis je Identitaet, kein konkurrierender Duplicate-Lauf. ``result_hash``
# gesetzt <=> atomarer Ergebnis-Commit existiert (fuer JFW-3/JFW-4 freigegeben
# nur bei status aligned/partially_aligned). Schreiben ausschliesslich ueber
# ``services/alignment_contract.py``.

class AlignmentResult(Base):
    """Versioniertes Forced-Alignment-Ergebnis, gebunden an Audio- und
    Transkriptrevision (JFW-2). Zeitgrenzen liegen in der Zeitbasis
    ``audio_ms_v1`` (Fließkomma-ms); der sichtbare Worttext bleibt exaktes
    Substring der gebundenen Revision (No-text-change-Vertrag).
    """
    __tablename__ = "alignment_results"
    id = Column(String, primary_key=True)  # uuid4
    identity_hash = Column(String, nullable=False, unique=True, index=True)
    payload_hash = Column(String, nullable=False)
    job_id = Column(String, nullable=False, index=True)
    attempt_id = Column(String, nullable=True)
    app_epoch = Column(String, nullable=True)
    audio_asset_id = Column(String, nullable=False)
    audio_hash = Column(String, nullable=False)
    audio_duration_ms = Column(Integer, nullable=False)
    timebase = Column(String, nullable=False, default="audio_ms_v1")
    transcript_run_id = Column(String, nullable=False)
    transcript_revision_id = Column(String, nullable=False)
    transcript_revision_hash = Column(String, nullable=False)
    language_ranges = Column(JSON, nullable=False, default=list)
    alignment_profile = Column(String, nullable=False)
    contract_version = Column(String, nullable=False)
    model_id = Column(String, nullable=True)
    model_revision = Column(String, nullable=True)
    model_sha256 = Column(String, nullable=True)
    model_license = Column(String, nullable=True)
    status = Column(String, nullable=False, default="queued")  # RESULT_STATES
    reason_code = Column(String, nullable=True)  # versioniert, inhaltsfrei
    words = Column(JSON, nullable=True)
    coverage_alignable = Column(Integer, nullable=True)
    coverage_aligned = Column(Integer, nullable=True)
    result_hash = Column(String, nullable=True)  # kanonischer Ergebnis-Hash
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    terminal_at = Column(DateTime, nullable=True)

# ── JFW-3: Diarisierungs-Ergebnisvertrag (Spec „Diarization Result Contract") ─
# Eine Ergebnisebene je Identitaet (job + audio + Transkriptrevision + JFW-2-
# Referenz + Profil + Sprecheranzahl + Vertragsversion). ``identity_hash`` ist
# UNIQUE — hoechstens ein autoritatives Ergebnis je Identitaet. ``result_hash``
# gesetzt <=> atomarer Ergebnis-Commit (fuer JFW-11/JFW-4 freigegeben nur bei
# status diarized/partially_diarized). Schreiben ausschliesslich ueber
# ``services/diarization_contract.py``. JFW-3 ergaenzt ausschliesslich
# Sprechercluster/Turns/Overlap-/Unsicherheitsstatus — nie Text oder Zeiten.

class DiarizationResult(Base):
    """Versioniertes Diarisierungsergebnis, gebunden an Audio-, Transkript- und
    JFW-2-Revision (JFW-3). Neutrale Cluster (``speaker_01``…) gelten nur
    innerhalb dieser Ergebnisrevision; Anzeigeetikette (``Sprecher 1``…) sind
    getrennt davon und behaupten keine Personenidentität.
    """
    __tablename__ = "diarization_results"
    id = Column(String, primary_key=True)  # uuid4
    identity_hash = Column(String, nullable=False, unique=True, index=True)
    payload_hash = Column(String, nullable=False)
    job_id = Column(String, nullable=False, index=True)
    attempt_id = Column(String, nullable=True)
    app_epoch = Column(String, nullable=True)
    audio_asset_id = Column(String, nullable=False)
    audio_hash = Column(String, nullable=False)
    audio_duration_ms = Column(Integer, nullable=False)
    timebase = Column(String, nullable=False, default="audio_ms_v1")
    transcript_run_id = Column(String, nullable=False)
    transcript_revision_id = Column(String, nullable=False)
    transcript_revision_hash = Column(String, nullable=False)
    jfw2_reference_status = Column(String, nullable=False)
    jfw2_result_hash = Column(String, nullable=True)
    speaker_mode = Column(String, nullable=False, default="auto")  # auto|exact|range
    speaker_count_min = Column(Integer, nullable=True)
    speaker_count_max = Column(Integer, nullable=True)
    diarization_profile = Column(String, nullable=False)
    contract_version = Column(String, nullable=False)
    model_id = Column(String, nullable=True)
    model_revision = Column(String, nullable=True)
    model_sha256 = Column(String, nullable=True)
    model_license = Column(String, nullable=True)
    status = Column(String, nullable=False, default="queued")  # RESULT_STATES
    reason_code = Column(String, nullable=True)  # versioniert, inhaltsfrei
    clusters = Column(JSON, nullable=True)
    turns = Column(JSON, nullable=True)
    words = Column(JSON, nullable=True)
    coverage_speech_ms = Column(Integer, nullable=True)
    coverage_usable_ms = Column(Integer, nullable=True)
    cluster_count = Column(Integer, nullable=True)
    turn_count = Column(Integer, nullable=True)
    word_count = Column(Integer, nullable=True)
    word_assigned_count = Column(Integer, nullable=True)
    overlap_count = Column(Integer, nullable=True)
    uncertainty_count = Column(Integer, nullable=True)
    result_hash = Column(String, nullable=True)  # kanonischer Ergebnis-Hash
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    terminal_at = Column(DateTime, nullable=True)
