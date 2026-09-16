"""JFW-12 (Spec B7/C): Vertragstests fuer den Task-/Lease-Datenvertrag.

Prueft den Exactly-once-Vertrag von ``backend/services/task_contract.py``:

- Finalisierung gewinnt -> genau eine autoritative Rohrevision, Terminalstatus.
- Cancel gewinnt -> keine Teilrevision, Audio bleibt uebernehmbar.
- Doppelte Finalisierung wird abgelehnt (already_terminal).
- Spate/verlorene Race-Seite schreibt nichts.
- Crash-Recovery: alte nichtterminale Attempts der vorigen Epoche -> failed;
  terminale Revisions bleiben unveraendert.

Ausfuehrung:  backend/.venv/Scripts/python.exe -m pytest tests/test_task_contract.py
"""
import sys
import threading
from pathlib import Path

import pytest
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.orm import sessionmaker

# ``database/models.py`` nutzt relative Imports (..utils.capture_chords) und muss
# daher als Teil des ``backend``-Pakets geladen werden — wie scripts/test_schema_contract.py.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from backend.database.models import Base
from backend.services.task_contract import (
    cancel_attempt,
    finalize_attempt,
    mark_failed,
    new_attempt,
    recover_stale_attempts,
)


@pytest.fixture
def session():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    factory = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    s = factory()
    try:
        yield s
    finally:
        s.close()
        engine.dispose()


def _attempt(session, **kw):
    defaults = dict(
        job_id="job-1",
        app_epoch="epoch-A",
        backend_generation=3,
        backend_variant="cpu",
        model_contract_hash="sha256:contract",
        input_hash="sha256:input",
    )
    defaults.update(kw)
    return new_attempt(session, **defaults)


def test_fresh_schema_has_task_tables():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    tables = set(inspect(engine).get_table_names())
    assert {"tasks", "transcript_revisions"} <= tables
    engine.dispose()


def test_finalize_writes_exactly_one_revision(session):
    a = _attempt(session)
    res = finalize_attempt(
        session, attempt_id=a.id, transcript_raw="hallo welt", stt_model="turbo"
    )
    assert res.outcome == "finalized"
    with engine_text(session) as conn:
        n = conn.execute(text("SELECT COUNT(*) FROM transcript_revisions")).scalar()
    assert n == 1
    session.refresh(a)
    assert a.status == "succeeded"
    assert a.terminal_at is not None


def test_double_finalize_rejected(session):
    a = _attempt(session)
    first = finalize_attempt(session, attempt_id=a.id, transcript_raw="erste")
    second = finalize_attempt(session, attempt_id=a.id, transcript_raw="zweite")
    assert first.outcome == "finalized"
    assert second.outcome == "already_terminal"
    with engine_text(session) as conn:
        n = conn.execute(text("SELECT COUNT(*) FROM transcript_revisions")).scalar()
    assert n == 1


def test_cancel_wins_no_partial_revision(session):
    a = _attempt(session)
    outcome = cancel_attempt(session, attempt_id=a.id)
    assert outcome == "cancelled"
    # Spae Finalisierung nach Cancel: abgelehnt, keine Teilrevision.
    res = finalize_attempt(session, attempt_id=a.id, transcript_raw="zu spaet")
    assert res.outcome == "cancelled"
    with engine_text(session) as conn:
        n = conn.execute(text("SELECT COUNT(*) FROM transcript_revisions")).scalar()
    assert n == 0
    session.refresh(a)
    assert a.status == "cancelled"


def test_cancel_after_finalize_rejected(session):
    a = _attempt(session)
    finalize_attempt(session, attempt_id=a.id, transcript_raw="fertig")
    outcome = cancel_attempt(session, attempt_id=a.id)
    assert outcome == "not_cancelled"
    session.refresh(a)
    assert a.status == "succeeded"


def test_mark_failed_conditional(session):
    a = _attempt(session)
    assert mark_failed(session, attempt_id=a.id, error_class="worker_crash") is True
    # Zweite Fehler-Transition wird abgelehnt (bereits terminal).
    assert mark_failed(session, attempt_id=a.id) is False


def test_concurrent_finalize_only_one_wins():
    """Race: zwei Threads finalisieren denselben Attempt — genau eine Revision."""
    import tempfile

    # Datei-DB (nicht :memory:): mehrere Verbindungen/Threads teilen dieselbe DB.
    db_path = Path(tempfile.mktemp(suffix=".db"))
    engine = create_engine(f"sqlite:///{db_path}", connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    factory = sessionmaker(autocommit=False, autoflush=False, bind=engine)

    setup = factory()
    a = _attempt(setup)
    attempt_id = a.id
    setup.close()

    results: list[str] = []
    barrier = threading.Barrier(2)

    def worker():
        s = factory()
        try:
            barrier.wait()
            res = finalize_attempt(s, attempt_id=attempt_id, transcript_raw="race")
            results.append(res.outcome)
        finally:
            s.close()

    threads = [threading.Thread(target=worker) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert results.count("finalized") == 1, f"genau eine Finalisierung erwartet: {results}"
    with engine.connect() as conn:
        n = conn.execute(text("SELECT COUNT(*) FROM transcript_revisions")).scalar()
    assert n == 1, f"genau eine autoritative Revision erwartet, gefunden {n}"
    engine.dispose()
    db_path.unlink(missing_ok=True)


def test_crash_recovery_stale_epoch(session):
    old_running = _attempt(session, app_epoch="epoch-OLD")
    old_pending = _attempt(session, app_epoch="epoch-OLD", job_id="job-2")
    current = _attempt(session)  # aktuelle Epoche: bleibt unangetastet
    finalize_attempt(session, attempt_id=current.id, transcript_raw="aktuell")

    n = recover_stale_attempts(session, current_epoch="epoch-A")
    assert n == 2
    session.refresh(old_running)
    session.refresh(old_pending)
    session.refresh(current)
    assert old_running.status == "failed"
    assert old_pending.status == "failed"
    assert current.status == "succeeded"  # terminale Revision bleibt unveraendert


def engine_text(session):
    """Kontextmanager: roher SQL-Zugriff auf die Session-Engine."""

    class _Ctx:
        def __enter__(self):
            self.conn = session.get_bind().connect()
            return self.conn

        def __exit__(self, *exc):
            self.conn.close()

    return _Ctx()
