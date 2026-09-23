"""ORM model definitions for the JF Whisper SQLite database.

JFW-1 (Transkriptions-Produktprofil): Die TTS-/LLM-Tabellen (profiles,
generations, stories, projects, effect_presets, audio_channels, ...) sind
aus dem Laufzeitvertrag entfernt. Das Schema-Kontrakt erlaubt exakt die
Tabellen ``captures`` und ``capture_settings`` -- alles andere darf nicht
materialisiert werden (Gate: scripts/verify_schema.py).
"""

from datetime import datetime
import uuid

from sqlalchemy import Column, String, Integer, DateTime, Text, Boolean, JSON, UniqueConstraint
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
    # JFW-7: Revisionstrennung raw_transcript | user_edited (nullable — Altzeilen
    # ohne revision_kind gelten als raw_transcript). user_edited ist eine getrennte
    # Kindrevision mit parent_revision_id; die Rohrevision bleibt unveraendert.
    revision_kind = Column(String, nullable=True, default="raw_transcript")
    parent_revision_id = Column(String, nullable=True)
    text_hash = Column(String, nullable=True)  # kanonischer Hash des exakten Textes
    segments = Column(JSON, nullable=True)  # Modelloutput-Segmente (unveraendert)
    provenance = Column(JSON, nullable=True)  # Sprache/Modell-/Backend-/Audio-Bindung
    run_identity_hash = Column(String, nullable=True)  # FK -> transcription_runs
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

# ── JFW-11: Dual-Source-Meeting-Ergebnisvertrag (Spec „Dual-Source Capture Contract") ─
# Eine Ergebnisebene je Identitaet (job + meeting-run + JFW-6-Run-Referenz +
# Vertragsversion). ``identity_hash`` ist UNIQUE — hoechstens ein autoritatives
# Ergebnis je Identitaet. ``result_hash`` gesetzt <=> atomarer Ergebnis-Commit
# (fuer JFW-4 freigegeben nur bei status secured_dual/secured_partial). Schreiben
# ausschliesslich ueber ``services/meeting_contract.py``. ``dedupe`` und
# ``name_mappings`` sind revisionsgebundene Annotationen auf unveraenderten
# JFW-2-/JFW-3-Ergebnissen — nie Text-, Wort- oder Clustermutation.

class MeetingResult(Base):
    """Versioniertes Dual-Source-Meeting-Ergebnis, gebunden an Capture-Manifest
    sowie JFW-2-/JFW-3-Revisionen (JFW-11). Zwei getrennte autoritative
    Rohspuren (mic/remote) bleiben in ``tracks`` mit Abschnitts-Hashes und
    eigener Provenienz referenziert; die gemeinsame Zeitbasis ist als versionierte
    Ableitung (Offset + Drift) in ``sync`` abgebildet.
    """
    __tablename__ = "meeting_results"
    id = Column(String, primary_key=True)  # uuid4
    identity_hash = Column(String, nullable=False, unique=True, index=True)
    payload_hash = Column(String, nullable=False)
    job_id = Column(String, nullable=False, index=True)
    meeting_run_id = Column(String, nullable=False)
    jfw6_run_reference = Column(String, nullable=False)
    contract_version = Column(String, nullable=False)
    jfw2_result_hash = Column(String, nullable=True)
    jfw3_result_hash = Column(String, nullable=True)
    manifest_hash = Column(String, nullable=False)
    timebase = Column(String, nullable=False, default="qpc_100ns")
    attempt_id = Column(String, nullable=True)
    app_epoch = Column(String, nullable=True)
    status = Column(String, nullable=False, default="queued")  # Run-Zustaende
    reason_code = Column(String, nullable=True)  # versioniert, inhaltsfrei
    stop_reason = Column(String, nullable=True)
    tracks = Column(JSON, nullable=True)
    gaps = Column(JSON, nullable=True)
    sync = Column(JSON, nullable=True)
    sound_cue_marks = Column(JSON, nullable=True)
    recovery_status = Column(JSON, nullable=True)
    dedupe = Column(JSON, nullable=True)
    name_mappings = Column(JSON, nullable=True)
    result_hash = Column(String, nullable=True)  # kanonischer Ergebnis-Hash
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    terminal_at = Column(DateTime, nullable=True)


class RecordingRun(Base):
    """Versionierter Diktat-Aufnahme-Run mit stabiler Identität (JFW-6).

    Genau eine Aufnahmeautorität (browserbasierte ``MediaRecorder``-Aufnahme);
    diese Tabelle haertet darunter Run-Identität, Exactly-once Start/Stopp,
    Formatbindung, Sound-Cue-Marken, Recovery-Status und den idempotenten
    JFW-7-Handoff. ``result_hash`` gesetzt <=> atomarer Ergebnis-Commit
    (``secured``); ``transcription_authorized`` wird genau einmal beim ersten
    Handoff gesetzt.
    """
    __tablename__ = "recording_runs"
    id = Column(String, primary_key=True)  # uuid4
    identity_hash = Column(String, nullable=False, unique=True, index=True)
    payload_hash = Column(String, nullable=False)
    run_id = Column(String, nullable=False, index=True)  # jfw6-run-<uuid4hex>
    contract_version = Column(String, nullable=False)
    device_stable_id_hash = Column(String, nullable=False)  # nie die volle Geraeteidentität
    format = Column(JSON, nullable=False)  # tatsaechlich geoeffnetes Format
    started_at_100ns = Column(Integer, nullable=False, default=0)
    attempt_id = Column(String, nullable=True)
    app_epoch = Column(String, nullable=True)
    status = Column(String, nullable=False, default="starting")  # Run-Zustaende
    reason_code = Column(String, nullable=True)  # versioniert, inhaltsfrei
    stop_reason = Column(String, nullable=True)  # Manifest-Stopgrund
    stop_cause = Column(String, nullable=True)
    stop_at_100ns = Column(Integer, nullable=True)  # genau ein angenommener Stopp-Intent
    audio_hash = Column(String, nullable=True)
    manifest_hash = Column(String, nullable=True)
    result_hash = Column(String, nullable=True)  # kanonischer Manifest-/Ergebnis-Hash
    manifest = Column(JSON, nullable=True)
    frames = Column(JSON, nullable=True)  # Frame-Bilanz angenommen = final + Luecke
    gaps = Column(JSON, nullable=True)
    sound_cue_marks = Column(JSON, nullable=True)
    recovery_status = Column(JSON, nullable=True)
    transcription_authorized = Column(Boolean, nullable=False, default=False)
    handoff_count = Column(Integer, nullable=False, default=0)  # genau eine Zustellung autorisiert
    deletion_contract_hash = Column(String, nullable=True)  # gebundener Löschvertrag
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    terminal_at = Column(DateTime, nullable=True)


# ── JFW-7: Transkriptions-Run (Processing Contract + Snapshot) ─────────────
# Eine Run-Ebene je Identitaet (Run-ID + Quellart + Ergebnisvertragsversion).
# ``identity_hash`` ist UNIQUE — identische erneute Zustellung ist idempotent,
# abweichender Payload fail-closed ``conflict``. ``snapshot_hash`` bindet den
# eingefrorenen Run-Snapshot (Audio-/Manifest-Hash, Modell + Revision, Sprache,
# Decode, JFW-12-Backendgeneration). ``attempts`` traegt die sichtbare
# Attempt-Liste inklusive Backendwechsel; genau ein terminaler Ausgang pro
# Attempt ueber bedingte DB-Transaktionen (Muster JFW-12 ``task_contract.py`` /
# JFW-6 ``recording_contract.py``) — Commit/Cancel-Race: der zuerst dauerhaft
# gespeicherte terminale Ausgang gewinnt. Schreiben ausschliesslich ueber
# ``services/transcription_contract.py``.

class TranscriptionRun(Base):
    """Versionierter Transkriptions-Run mit eingefrorenem Snapshot (JFW-7).

    ``result_hash`` gesetzt <=> atomarer Rohtranskript-Commit (``raw_ready``);
    ``no_speech``/``failed``/``canceled``/``invalidated`` erzeugen keinen
    JFW-8-Payload. Genau eine autoritative Rohrevision pro Attempt liegt in
    ``transcript_revisions`` (``source_attempt_id`` UNIQUE).
    """
    __tablename__ = "transcription_runs"
    id = Column(String, primary_key=True)  # uuid4
    identity_hash = Column(String, nullable=False, unique=True, index=True)
    payload_hash = Column(String, nullable=False)
    run_id = Column(String, nullable=False, index=True)  # jfw7-run-/upload-/retranscribe-<uuid4hex>
    source_kind = Column(String, nullable=False)  # jfw6_handoff | upload | retranscribe
    contract_version = Column(String, nullable=False)  # dictation_raw_v1
    audio_hash = Column(String, nullable=False)
    manifest_hash = Column(String, nullable=True)  # JFW-6-Handoff; Upload ohne Manifest
    capture_id = Column(String, nullable=True)
    stop_reason = Column(String, nullable=True)  # Stopgrund aus dem JFW-6-Manifest
    snapshot = Column(JSON, nullable=False)  # eingefrorener Run-Snapshot
    snapshot_hash = Column(String, nullable=False)
    stt_model = Column(String, nullable=True)
    model_revision = Column(String, nullable=True)  # immutable 40-Hex-Revision
    language_setting = Column(String, nullable=True)  # auto | de | en
    backend_variant = Column(String, nullable=True)  # cpu | cuda (Attempt-Bindung)
    backend_generation = Column(Integer, nullable=True)  # JFW-12-Generation
    attempt_id = Column(String, nullable=True)  # aktuell gebundener Attempt
    attempts = Column(JSON, nullable=True)  # sichtbare Attempt-Liste
    status = Column(String, nullable=False, default="queued")  # Processing Contract
    reason_code = Column(String, nullable=True)  # versioniert, inhaltsfrei
    app_epoch = Column(String, nullable=True)
    revision_id = Column(String, nullable=True)  # autoritative raw_transcript-Revision
    text_hash = Column(String, nullable=True)
    result_hash = Column(String, nullable=True)  # kanonischer Ergebnis-Hash
    duration_ms = Column(Integer, nullable=True)
    cancel_requested_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    terminal_at = Column(DateTime, nullable=True)


# ── JFW-8: Delivery-Operation (sichere Zieluebergabe + Recovery) ──────────
# ``identity_hash`` ist UNIQUE — identische erneute Zustellung ist idempotent,
# abweichender Payload fail-closed ``conflict``. ``auto_attempt_consumed`` ist
# das dauerhaft verbrauchte Einmalbudget (Exactly-once, VOR externer Eingabe
# gespeichert); genau ein terminaler Ausgang pro Operation ueber bedingte
# DB-Transaktionen. ``recovery_text`` ist der benutzergebundene, zeitlich
# begrenzte Rohtext — gemeinsam mit den sensiblen Zwi­schenstaenden geloescht;
# der danach verbleibende Tombstone ist inhaltsfrei. Schreiben ausschliesslich
# ueber ``services/delivery_contract.py``.

class DeliveryOperation(Base):
    """Versionierte Delivery-Operation der appuebergreifenden Zieluebergabe (JFW-8).

    ``attempt_intent`` und ``error_trace`` sind konstruktiv inhaltsfrei
    (IDs, Adapter, Zustaende, Dauer, Fehlercodes — nie Rohtext, Clipboard,
    Fenstertitel mit Inhalt oder Credential).
    """
    __tablename__ = "delivery_operations"
    id = Column(String, primary_key=True)  # uuid4
    identity_hash = Column(String, nullable=False, unique=True, index=True)
    payload_hash = Column(String, nullable=False)
    delivery_operation_id = Column(String, nullable=False, index=True)  # jfw8-op-<uuid4hex>
    parent_operation_id = Column(String, nullable=True)  # append-only Kindbezug
    contract_version = Column(String, nullable=False)  # delivery_operation_v1
    run_id = Column(String, nullable=False)  # JFW-6/JFW-7-Run-Identitaet
    audio_hash = Column(String, nullable=False)
    jfw7_attempt_id = Column(String, nullable=True)
    revision_id = Column(String, nullable=True)  # JFW-7 raw_transcript-Revision
    text_hash = Column(String, nullable=False)
    target_snapshot = Column(JSON, nullable=True)  # inhaltsfreie Zielidentitaet
    target_confirmed = Column(Boolean, nullable=False, default=False)
    capability = Column(String, nullable=True)  # direct_text|verified_paste|manual_only|blocked
    status = Column(String, nullable=False, default="received")  # Delivery State Contract
    auto_attempt_consumed = Column(Boolean, nullable=False, default=False)
    attempt_intent = Column(JSON, nullable=True)  # inhaltsfrei
    attempt_id = Column(String, nullable=True)
    app_epoch = Column(String, nullable=True)
    reason_code = Column(String, nullable=True)  # versioniert, inhaltsfrei
    recovery_text = Column(Text, nullable=True)  # benutzergebundener Rohtext
    recovery_expires_at = Column(DateTime, nullable=True)
    error_trace = Column(JSON, nullable=True)  # inhaltsfreie Fehlerpfad-Spur
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    terminal_at = Column(DateTime, nullable=True)


class ExportJob(Base):
    """Versionierter Exportauftrag fuer Sprecher-/Wortzeiten-Exporte (JFW-4).

    ``export_key`` ist der deterministische Export-Schluessel (ohne Zielbindung),
    ``payload_hash`` bindet zusaetzlich Zielidentitaet und erwartete Dateinamen.
    ``result_hash`` gesetzt <=> autoritativer Set-Commit; das ``manifest`` traegt
    die tatsaechlichen Inhalts-Hashes aller Praesentationsdateien.
    """
    __tablename__ = "export_jobs"
    id = Column(String, primary_key=True)  # uuid4
    export_key = Column(String, nullable=False, unique=True, index=True)
    payload_hash = Column(String, nullable=False)
    contract_version = Column(String, nullable=False)  # jfw4_export_v1
    job_id = Column(String, nullable=False, index=True)
    audio_asset_id = Column(String, nullable=False)
    audio_hash = Column(String, nullable=False)
    audio_duration_ms = Column(Integer, nullable=False)
    timebase = Column(String, nullable=False)
    transcript_run_id = Column(String, nullable=False)
    transcript_revision_id = Column(String, nullable=False)
    transcript_revision_hash = Column(String, nullable=False)
    jfw2_result_hash = Column(String, nullable=False)
    jfw2_status = Column(String, nullable=False)
    jfw3_result_hash = Column(String, nullable=False)
    jfw3_status = Column(String, nullable=False)
    jfw11_commit_hash = Column(String, nullable=True)
    jfw11_status = Column(String, nullable=True)
    formats = Column(JSON, nullable=False)  # ["json", ...]
    export_profile = Column(String, nullable=False)
    name_policy = Column(String, nullable=False)
    partial_mode = Column(String, nullable=True)
    partial_confirmed = Column(Boolean, nullable=False, default=False)
    target_dir = Column(String, nullable=True)
    expected_files = Column(JSON, nullable=True)
    status = Column(String, nullable=False, default="preparing")  # Export Result Contract
    reason_code = Column(String, nullable=True)  # versioniert, inhaltsfrei
    readiness = Column(JSON, nullable=True)  # Readiness inkl. Warnungen
    manifest = Column(JSON, nullable=True)  # Praesentationsdatei-Hashes
    result_hash = Column(String, nullable=True)  # gesetzt <=> exported
    attempt_id = Column(String, nullable=True)
    app_epoch = Column(String, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    terminal_at = Column(DateTime, nullable=True)


class MinutesResult(Base):
    """Versionierter Protokollauftrag fuer Meeting-Protokolle (JFW-13).

    ``minutes_key`` bindet deterministisch alle Eingangsrevisionen (JFW-4-Snapshot
    inkl. JFW-2/JFW-3/(optional) JFW-11) UND die bestaetigte Registerrevision —
    genau ein autoritatives Ergebnis je Schluessel (Byte-Regel). ``payload_hash``
    bindet zusaetzlich Zielidentitaet/Dateinamen. ``result_hash`` gesetzt <=>
    atomarer Ergebnis-Commit; ``nondeterminism_revisions`` faehrt nicht
    reproduzierbare Modellantworten als gesondert versionierte Ergebnisrevisionen
    (NIE als zweite Fassung). Schreiben ausschliesslich ueber
    ``services/minutes_contract.py``.
    """
    __tablename__ = "minutes_results"
    id = Column(String, primary_key=True)  # uuid4
    minutes_key = Column(String, nullable=False, unique=True, index=True)
    payload_hash = Column(String, nullable=False)
    contract_version = Column(String, nullable=False)  # jfw13_minutes_v1
    minutes_profile = Column(String, nullable=False)
    register_id = Column(String, nullable=False, index=True)
    register_revision = Column(String, nullable=False)
    job_id = Column(String, nullable=False, index=True)
    audio_asset_id = Column(String, nullable=False)
    audio_hash = Column(String, nullable=False)
    audio_duration_ms = Column(Integer, nullable=False)
    timebase = Column(String, nullable=False)
    transcript_run_id = Column(String, nullable=False)
    transcript_revision_id = Column(String, nullable=False)
    transcript_revision_hash = Column(String, nullable=False)
    transcript_text_hash = Column(String, nullable=False)
    jfw2_result_hash = Column(String, nullable=False)
    jfw2_status = Column(String, nullable=False)
    jfw3_result_hash = Column(String, nullable=False)
    jfw3_status = Column(String, nullable=False)
    jfw4_export_key = Column(String, nullable=False)
    jfw4_result_hash = Column(String, nullable=False)
    jfw11_commit_hash = Column(String, nullable=True)
    jfw11_status = Column(String, nullable=True)
    status = Column(String, nullable=False, default="queued")  # Protokoll Result Contract
    reason_code = Column(String, nullable=True)  # versioniert, inhaltsfrei
    readiness = Column(JSON, nullable=True)  # Readiness inkl. Warnungen
    warnings = Column(JSON, nullable=True)  # sichtbare Warnliste (inhaltfrei)
    model_provenance = Column(JSON, nullable=True)  # Modell-/Artefaktprovenienz
    document = Column(JSON, nullable=True)  # Protokolldokument jfw13_minutes_v1
    result_hash = Column(String, nullable=True)  # gesetzt <=> autoritativer Commit
    nondeterminism_revisions = Column(JSON, nullable=True)
    attempt_id = Column(String, nullable=True)
    app_epoch = Column(String, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    terminal_at = Column(DateTime, nullable=True)


class PseudonymRegister(Base):
    """Revisionierte Zuordnungsinformation (JFW-13) — getrennt vom Protokoll.

    Ausdruecklich und vollstaendig loeschbar (``delete_register`` entfernt die
    Zuordnungsinformation; Pseudonyme im Protokoll bleiben gueltig). Nie Inhalt
    von Logs, Metriken, Crash-Dumps oder Standard-Exporten.
    """
    __tablename__ = "pseudonym_registers"
    id = Column(String, primary_key=True)  # uuid4 (Zeile)
    register_id = Column(String, nullable=False, index=True)
    revision = Column(Integer, nullable=False)
    revision_id = Column(String, nullable=False)
    parent_revision_id = Column(String, nullable=True)
    minutes_key = Column(String, nullable=True, index=True)
    status = Column(String, nullable=False)  # vorgeschlagen|bestaetigt|...|geloescht
    entries = Column(JSON, nullable=False)  # Zuordnungsinformation (sensibel)
    created_at = Column(DateTime, default=datetime.utcnow)
    deleted_at = Column(DateTime, nullable=True)
    __table_args__ = (
        UniqueConstraint("register_id", "revision", name="uq_pseudonym_registers_rev"),
    )


# ── JFW-5: Batch-Auftragsmodell (Batch-Result / Batch-Item Contract) ──────
# ``batches`` traegt GENAU EINE unveraenderliche Snapshot-Revision je Zeile
# (``identity_hash`` UNIQUE + ``payload_hash`` fail-closed); ``batch_items``
# bindet jedes Element an Quell-, Profil- und Snapshotrevision samt
# autoritativer Ergebnisreferenzen; ``batch_attempts`` fuehrt je Element die
# nachvollziehbaren Versuche inklusive ``interrupted`` (fortsetzbar, nie
# fachlicher Erfolg). Schreiben ausschliesslich ueber
# ``services/batch_contract.py``.

#: Verbindliche Batch-Zustaende (Spec „Batch Result Contract").
BATCH_STATUSES = (
    "draft", "validating", "ready", "running", "pausing", "paused",
    "completed", "completed_with_issues", "canceled", "blocked",
)
#: Element-Endzustaende je aktuellem Versuch (Spec „Batch Item Contract").
ITEM_END_STATES = (
    "succeeded", "succeeded_with_warnings", "failed", "canceled",
    "blocked", "interrupted", "invalidated",
)


class Batch(Base):
    """Unveraenderliche Snapshot-Revision eines bestaetigten Batches (JFW-5)."""
    __tablename__ = "batches"
    id = Column(String, primary_key=True)  # uuid4 (Zeile)
    identity_hash = Column(String, nullable=False, unique=True, index=True)
    payload_hash = Column(String, nullable=False)
    batch_id = Column(String, nullable=False, index=True)  # jfw5-batch-<uuid4hex>
    contract_version = Column(String, nullable=False)  # jfw5_batch_v1
    revision_no = Column(Integer, nullable=False, default=1)
    parent_snapshot_hash = Column(String, nullable=True)
    snapshot_hash = Column(String, nullable=False, index=True)
    snapshot = Column(JSON, nullable=False)  # eingefrorene Auswahl + Quellen
    profile = Column(JSON, nullable=False)  # eingefrorene gemeinsame Profilrevision
    profile_hash = Column(String, nullable=False)
    phases = Column(JSON, nullable=False)  # bestaetigte Phasenreihenfolge
    partial_failure_policy = Column(String, nullable=False)
    resource_policy = Column(JSON, nullable=False)  # jfw5_serial_v1
    output_policy = Column(JSON, nullable=False)
    frozen_order = Column(JSON, nullable=False)  # eingefrorene Reihenfolge
    status = Column(String, nullable=False, default="ready")  # BATCH_STATUSES
    reason_code = Column(String, nullable=True)  # versioniert, inhaltsfrei
    app_epoch = Column(String, nullable=True)
    aggregates = Column(JSON, nullable=True)  # abgeleitete Uebersicht (nie Autoritaet)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    terminal_at = Column(DateTime, nullable=True)


class BatchItem(Base):
    """Batch-Element mit Quell-/Profilbindung und autoritativen Ergebnisreferenzen.

    ``result_refs`` haelt ausschliesslich Referenzen (identity/result hash) auf
    die Commits der gebundenen Feature-Vertraege — kein Batchfehler loescht
    einen gueltigen Elementcommit.
    """
    __tablename__ = "batch_items"
    id = Column(String, primary_key=True)  # uuid4 (Zeile)
    identity_hash = Column(String, nullable=False, unique=True, index=True)
    payload_hash = Column(String, nullable=False)
    batch_identity_hash = Column(String, nullable=False, index=True)
    batch_id = Column(String, nullable=False, index=True)
    snapshot_hash = Column(String, nullable=False)
    item_id = Column(String, nullable=False, index=True)  # jfw5-item-<16hex>
    order_index = Column(Integer, nullable=False)
    source = Column(JSON, nullable=False)  # Quellenidentitaet + Inhaltsnachweis
    relative_path = Column(String, nullable=False)
    selection_refs = Column(JSON, nullable=False)
    profile_hash = Column(String, nullable=False)
    status = Column(String, nullable=False, default="waiting")  # ITEM_END_STATES | waiting|active
    current_phase = Column(String, nullable=True)
    phases = Column(JSON, nullable=True)  # Phasenzustaende des aktuellen Versuchs
    result_refs = Column(JSON, nullable=True)  # autoritative Ergebnisreferenzen je Phase
    warnings = Column(JSON, nullable=True)
    output = Column(JSON, nullable=True)  # deterministische Zielzuordnung
    attempt_count = Column(Integer, nullable=False, default=0)
    reason_code = Column(String, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    terminal_at = Column(DateTime, nullable=True)


class BatchAttempt(Base):
    """Einzelner Verarbeitungsversuch eines Batch-Elements (JFW-5).

    Stabile Versuch-ID, Start-/Endstatus inklusive ``interrupted``,
    Eingangsrevisionen, wiederverwendete Commits und konkreter Fehler-/
    Abbruchgrund. UNIQUE (batch, item, Versuchsnummer) verhindert doppelte
    Versuche bei konkurrierender Finalisierung/Cancel.
    """
    __tablename__ = "batch_attempts"
    id = Column(String, primary_key=True)  # uuid4 (Zeile)
    attempt_id = Column(String, nullable=False, unique=True, index=True)  # jfw5-attempt-<uuid4hex>
    batch_identity_hash = Column(String, nullable=False, index=True)
    batch_id = Column(String, nullable=False, index=True)
    item_id = Column(String, nullable=False, index=True)
    attempt_no = Column(Integer, nullable=False)
    status = Column(String, nullable=False, default="active")  # active|pending|...|terminal
    input_revisions = Column(JSON, nullable=True)  # Quell-/Profil-/Snapshotrevision
    reused_commits = Column(JSON, nullable=True)  # wiederverwendete autoritative Commits
    phase_states = Column(JSON, nullable=True)
    reason_code = Column(String, nullable=True)
    app_epoch = Column(String, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    terminal_at = Column(DateTime, nullable=True)
    __table_args__ = (
        UniqueConstraint("batch_identity_hash", "item_id", "attempt_no",
                         name="uq_batch_attempts_item_no"),
    )
