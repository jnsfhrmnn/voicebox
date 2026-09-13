"""CPU-Early-Entrypoint fuer die JFW-1 DB-Schema-Linie (kanonisch).

Dieses Modul ist der einzige Migrationspfad des Produkts und darf OHNE das
``backend``-Paket importiert werden — es laedt Alembic direkt aus der Datei
und fuehrt ``upgrade head`` gegen die SQLite-Datei aus. Damit ist die
Schema-Linie auch ohne installiertes Backend (und damit ohne torch) lauffaehig:

* Tauri ruft sie vor dem Sidecar-Start auf (Server-Binary: ``--migrate <db>``),
* Build-/CI-Schritte koennen sie direkt importieren,
* der gebuendelte Server-Binary traegt dieselbe Logik (PyInstaller bundelt
  das Modul als normalen Python-Code).

Die Revision-Kette wird aus ``alembic/versions`` abgeleitet — ein einziger
Source of Truth. Das Verzeichnis liegt neben diesem Modul und wird ueber
``__file__`` aufgelöst, daher ist der Aufruf CWD-unabhaengig (Quellbaum und
PyInstaller-Binary).

Verhalten (Spec JFW-1, DB-Migrations-Kontrakt):

* **Lock/Backup:** Vor jeder Migration wird die DB-Datei nach
  ``<data_dir>/backups/voicebox.db.<ts>.bak`` kopiert (letzte 5 behalten);
  das Upgrade laeuft in einer Alembic-Transaktion.
* **Fail-closed:** Ein unbekannter, verzweigter oder neuerer Head (Revision
  in ``alembic_version``, die nicht Teil der gebuendelten Kette ist) stoppt
  den Start — weder CPU noch CUDA starten auf dieser DB. Die Backup-Kopie
  bleibt erhalten und der Zustand ist recoverbar.
* **Idempotenz:** Wiederholter Start auf demselben Head ist ein No-Op.
* **Manifest-Daten:** ``schema_meta`` traegt Schema-ID, Head und
  Migrationshash — die Artefaktmanifeste (CPU/CUDA) melden dieselben Werte;
  eine Abweichung wird beim Start abgelehnt.

Aufruf::

    python backend/schema.py /pfad/zur/data/voicebox.db          # Migration
    python backend/schema.py --info /pfad/zur/data/voicebox.db   # nur Status
"""

import hashlib
import os
import shutil
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

# Schema-Kontrakt (Profil: product-profiles/jf-whisper.json -> database_tables).
SCHEMA_ID = "jfwhisper-v1"
ALLOWED_TABLES = ("captures", "capture_settings")
BACKUP_KEEP = 5


class MigrationError(RuntimeError):
    """Fail-closed Migrationsfehler — der Start darf nicht fortgesetzt werden."""


# ---------------------------------------------------------------------------
# Revision-Kette (aus alembic/versions abgeleitet)
# ---------------------------------------------------------------------------

def _script_dir() -> "object":
    from alembic.config import Config as AlembicConfig  # type: ignore[import-untyped]
    from alembic.script import ScriptDirectory  # type: ignore[import-untyped]

    backend_dir = Path(__file__).resolve().parent
    cfg = AlembicConfig(str(backend_dir / "alembic.ini"))
    # script_location aus __file__ auflösen — die ini trägt nur den relativen
    # Pfad, damit das Repo auf jeder Maschine identisch bleibt.
    cfg.set_main_option("script_location", str(backend_dir / "alembic"))
    return ScriptDirectory.from_config(cfg)


def _chain() -> list[tuple[str, str | None]]:
    """Die Revision-Kette als (revision, down_revision)-Paare, alt → neu."""
    script = _script_dir()
    chain: list[tuple[str, str | None]] = []
    for rev in script.walk_revisions():
        chain.append((rev.revision, rev.down_revision))
    return chain


def head_rev() -> str:
    return _script_dir().get_current_head()


def known_revs() -> set[str]:
    return {r[0] for r in _chain()}


def migration_hash() -> str:
    """Stabiler Hash ueber die Revision-Kette (Manifest-Vergleich CPU/CUDA)."""
    h = hashlib.sha256()
    for rev, down in _chain():
        h.update(f"{rev}->{down}:".encode("utf-8"))
    return h.hexdigest()[:16]


# ---------------------------------------------------------------------------
# DB-Zugriff (direkt via sqlite3 — kein SQLAlchemy-Pool, kein Lock-Risiko)
# ---------------------------------------------------------------------------

def _connect(db_path: Path) -> sqlite3.Connection:
    con = sqlite3.connect(str(db_path), timeout=10.0)
    con.execute("PRAGMA busy_timeout = 10000")
    return con


def current_rev(con: sqlite3.Connection) -> str | None:
    """Aktuelle Revision aus der DB (None bei frischer/legacy-DB ohne Alembic)."""
    tables = {r[0] for r in con.execute(
        "SELECT name FROM sqlite_master WHERE type='table'"
    )}
    if "alembic_version" not in tables:
        return None
    row = con.execute("SELECT version_num FROM alembic_version").fetchone()
    return row[0] if row else None


def _backup(db_path: Path) -> Path | None:
    """Kopiert die DB-Datei nach <data_dir>/backups/ (letzte BACKUP_KEEP)."""
    data_dir = db_path.parent
    backup_dir = data_dir / "backups"
    if not db_path.exists():
        return None
    backup_dir.mkdir(parents=True, exist_ok=True)
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    target = backup_dir / f"voicebox.db.{ts}.bak"
    shutil.copy2(db_path, target)
    # Alte Backups aufräumen (neueste BACKUP_KEEP behalten).
    backups = sorted(backup_dir.glob("voicebox.db.*.bak"))
    for old in backups[:-BACKUP_KEEP]:
        try:
            old.unlink()
        except OSError:
            pass
    return target


def _write_schema_meta(con: sqlite3.Connection) -> None:
    con.execute(
        "CREATE TABLE IF NOT EXISTS schema_meta ("
        " key TEXT PRIMARY KEY, value TEXT NOT NULL)"
    )
    for key, value in (
        ("schema_id", SCHEMA_ID),
        ("head", head_rev()),
        ("migration_hash", migration_hash()),
    ):
        con.execute(
            "INSERT INTO schema_meta (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value),
        )


# ---------------------------------------------------------------------------
# Öffentliche API
# ---------------------------------------------------------------------------

def run_schema_upgrade(db_path=None, engine=None) -> None:
    """Fuehrt ``upgrade head`` gegen die gegebene DB-Datei aus.

    ``engine`` (optional): eine bereits existierende SQLAlchemy-Engine auf
    dieselbe Datei — wird vor dem Upgrade disposed, damit keine pooled
    Connections das SQLite-Lock blockieren (Windows: "database is locked").

    Wirft :class:`MigrationError` bei fail-closed Bedingungen; der Aufrufer
    darf den Start danach NICHT fortsetzen.
    """
    if db_path is None:
        url_env = os.environ.get("JFWHISPER_DB_URL")
        if not url_env:
            raise MigrationError(
                "Kein DB-Pfad: Argument oder JFWHISPER_DB_URL setzen."
            )
        db_path = Path(url_env.removeprefix("sqlite:///"))
    db_path = Path(db_path)

    # Pooled Connections loesen, bevor wir selbst verbinden.
    if engine is not None:
        try:
            engine.dispose()
        except Exception:  # noqa: BLE001 -- dispose darf nicht killen
            pass

    con = _connect(db_path)
    try:
        rev_before = current_rev(con)

        # Fail-closed: unbekannter, verzweigter oder neuerer Head.
        if rev_before is not None and rev_before not in known_revs():
            raise MigrationError(
                f"Unbekannte Schema-Revision {rev_before!r} (bekannt: "
                f"{sorted(known_revs())}). Start abgelehnt — DB bleibt "
                "unverändert, Backup liegt in backups/."
            )

        if rev_before == head_rev():
            # Idempotenz: nur Schema-Meta aktualisieren (kein Upgrade).
            _write_schema_meta(con)
            con.commit()
            return

        backup = _backup(db_path)
        try:
            from alembic import command  # type: ignore[import-untyped]
            from alembic.config import Config as AlembicConfig  # type: ignore[import-untyped]
        except ImportError as exc:  # pragma: no cover -- Build-Fehler
            raise MigrationError(
                f"Alembic ist nicht im Binary gebundlet ({exc}) — "
                "Schema-Linie nicht lauffähig."
            ) from exc

        backend_dir = Path(__file__).resolve().parent
        os.environ["JFWHISPER_DB_URL"] = f"sqlite:///{db_path}"
        cfg = AlembicConfig(str(backend_dir / "alembic.ini"))
        cfg.set_main_option("script_location", str(backend_dir / "alembic"))

        try:
            command.upgrade(cfg, "head")
        except MigrationError:
            raise
        except Exception as exc:  # noqa: BLE001 -- Abbruch = fail-closed
            raise MigrationError(
                f"Migration abgebrochen ({exc}). Backup: {backup}. "
                "DB-Zustand ist recoverbar."
            ) from exc

        # Nach dem Upgrade: Meta schreiben + Kontrakt-Check.
        _write_schema_meta(con)
        con.commit()

        tables = {r[0] for r in con.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )}
        forbidden = tables - set(ALLOWED_TABLES) - {"alembic_version", "schema_meta"}
        if forbidden:
            raise MigrationError(
                f"Schema-Kontrakt verletzt nach Upgrade: unerwartete Tabellen "
                f"{sorted(forbidden)}. Backup: {backup}."
            )
    finally:
        con.close()


def schema_info(db_path) -> dict:
    """Liest Schema-Meta + aktuelle Revision (für --info / Manifeste)."""
    db_path = Path(db_path)
    info = {"db": str(db_path), "exists": db_path.exists()}
    if not db_path.exists():
        return info
    con = _connect(db_path)
    try:
        info["current_rev"] = current_rev(con)
        tables = {r[0] for r in con.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )}
        if "schema_meta" in tables:
            for key, value in con.execute("SELECT key, value FROM schema_meta"):
                info[key] = value
    finally:
        con.close()
    return info


if __name__ == "__main__":
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    flags = {a for a in sys.argv[1:] if a.startswith("--")}
    try:
        if "--info" in flags:
            import json

            print(json.dumps(schema_info(args[0] if args else None), indent=2))
        else:
            run_schema_upgrade(args[0] if args else None)
            print("schema upgrade OK")
    except MigrationError as exc:
        print(f"SCHEMA-FAIL-CLOSED: {exc}", file=sys.stderr)
        sys.exit(3)
