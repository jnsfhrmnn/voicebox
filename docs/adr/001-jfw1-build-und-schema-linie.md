# ADR 001: JFW-1 Build-Namen, DB-Pfad und Schema-Linie

Status: Akzeptiert (2026-09-13) — Umsetzung Block (d), Fork `foundation/v0.5.0`

## Kontext

Die Spec (JFW-1, Abschnitt "Build und Paketierung" + "Prozess- und Sicherheitsgrenze")
verlangt eine eigene App-Identität: neue Namen, Pfade, Port- und Updategrenzen. Die
produktive Voicebox-Installation darf nicht gestartet, beendet, überschrieben oder
migriert werden. Der Upstream-Fork trug noch die Voicebox-Namen (`voicebox-server`,
`data/voicebox.db`) und eine zu breite Capability-Oberfläche.

## Entscheidung

1. **Sidecar-Binary:** `jf-whisper-server` (CPU, onefile) bzw.
   `jf-whisper-server-cuda` (CUDA, onedir — JFW-1 nur lokal gebaut/getestet).
   PyInstaller-Namen, `externalBin`, Build-Skripte und Rust-Pfadauflösung tragen den
   neuen Namen; der Upstream-Name `voicebox-server` wird nicht mehr erzeugt.

2. **Datenroot:** `%LOCALAPPDATA%\JFWhisper` (Profil: `app_identity.data_root`).
   Die Runtime-DB liegt in `%LOCALAPPDATA%\JFWhisper\data\jf-whisper.db`. Tauri
   übergibt `--data-dir %LOCALAPPDATA%\JFWhisper\data`; der DB-Filename ist
   `jf-whisper.db` (nicht mehr `voicebox.db`). Captures liegen unter
   `%LOCALAPPDATA%\JFWhisper\captures`.

3. **Schema-Linie:** Alembic ist die kanonische Schema-Quelle
   (`backend/alembic.ini`, `backend/alembic/versions/`). Der CPU-Early-Entrypoint
   `backend/schema.py` (ohne backend-Paket, ohne torch) bietet:
   - `--schema-status`: meldet Head, aktuelle Revision, Migrationskettenhash und
     Schema-ID als JSON; kein DB-Schreibzugriff.
   - `--migrate-only`: exklusiver Upgrade-Lauf mit WAL-Checkpoint, SQLite-Backup,
     Alembic-Batchmodus, abschließendem Head-/Allowlist-/Foreign-Key-/Integritätscheck.
   Tauri vergleicht den erwarteten Schema-Head (aus dem Artefaktmanifest) über
   `--schema-status` VOR jedem Serverstart; Abweichung = Start abgelehnt (fail-closed).

4. **Locks:** Tauri hält eine Single-Instance-Lock pro Datenroot
   (`%LOCALAPPDATA%\JFWhisper\.instance.lock`). Jeder normale DB-Opener hält eine
   gemeinsame OS-Schemalock für seine gesamte DB-Lebensdauer; Migration benötigt
   exklusiven Zugriff.

5. **Migrationskettenhash:** topologisch geordnetes kanonisches Manifest aus
   Revision/`down_revision`, POSIX-Relativpfad und SHA-256 jedes Migrations-/
   Metadatenblobs (UTF-8, LF erzwungen); sortiertes kanonisches JSON erzeugt in
   CPU- und CUDA-Builds denselben Hash.

6. **CUDA:** Voicebox-CUDA-Download/-Installation und `check_and_update_cuda_binary()`
   sind bis JFW-12 fail-closed aus dem aktiven Produktprofil entfernt; der Windows-
   Installer enthält nur das CPU-Sidecar.

7. **Capabilities:** Fenster erhalten getrennte minimale Capabilities
   (`capabilities/main.json`, `capabilities/dictate.json`); die breite Upstream-
   `default.json` (fs:read-all/write-all, shell spawn/execute) wird entfernt.
   Shell darf nur die deklarierten Sidecars mit validierten Argumenten starten;
   `shell.open: "*"` ist weg.

## Konsequenzen

- Rename betrifft Build-Skripte, tauri.conf.json, main.rs (Pfadauflösung),
  server.py und build_binary.py — alle Referenzen werden in einem Commit geändert.
- Die Legacy-Konvergenz (Voicebox-DB → JFW-1-Kontrakt) bleibt erhalten: eine
  vorhandene `voicebox.db` wird beim ersten Start nach `jf-whisper.db` migriert,
  nicht überschrieben.
