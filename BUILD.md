# Build- und Artefakt-Vertrag (jf-whisper / voicebox-Fork)

Gilt für `application/` (Submodule, Fork `jnsfhrmnn/voicebox`) und die Release-Werkzeuge
unter `E:\jf-whisper\release-assets\tools\`. Änderungen an der Werkzeugkette nur hier
dokumentiert einführen — reproduzierbare Artefakte sind Vertrag, keine Routine.

## Build-Vertrag Sidecar (USCRX-2026-16038)

- **Einzig zulässiger Build-Weg:** `python backend/build_binary.py` (CPU) bzw.
  `python backend/build_binary.py --cuda` (JFW-1-Profil, `--onedir`). Kein
  direkter `pyinstaller <spec>`-Aufruf, kein Bau über eine liegengelassene
  generierte Spec. Vorfall 2026-09-24: ein Seitenkanal-Bau über
  `jf-whisper-server.spec` mit Python 3.12 ersetzte die Werkzeugkette und
  brachte den Server zum Ausfall — die Ersatzkette `build_binary.py` stellte
  den Betrieb wieder her.
- **console=True ist die Produktionsvariante.** Die App liest stdout/stderr des
  Sidecars über Pipes (`tauri/src-tauri/src/backend/process_windows.rs`,
  `STARTF_USESTDHANDLES`) und zeigt sie im Server-Log. **console=False zerstört
  die Ausgabe unter der App:** PyInstaller `--noconsole` lässt
  `sys.stdout`/`sys.stderr` None/broken zurück, der Guard am Kopf von
  `backend/server.py` schreibt dann auf `os.devnull` — Logs, Fehler und
  Fortschritt verschwinden. Ein Konsolenfenster kostet `--console` dabei nicht:
  der Spawn läuft aus einer GUI-App ohne Konsolen-Handle, es entsteht kein
  Kindfenster. Die Specs tragen die Begründung als Kommentar an `console=`.

## Deploy-Kette Sidecar (atomar, USCRX-2026-16106)

Werkzeug: `python scripts/deploy_sidecar.py --target-dir <App-Exe-Dir>`
(bauen → prüfen → **ein einziger Tauschschritt** → Deploy-Check). Nur prüfen:
`--check`. Einzelheiten siehe „Betriebsdisziplin“ unten.

## CUDA-Artefakt (Addon) — Installationspfad und Vertrag

**Installationspfad (produktiv):**
`C:\Users\jensf\AppData\Local\JFWhisper\backends\cuda\<build_id>\jf-whisper-server-cuda.exe`
(OneDir-Layout, `\_internal\` direkt daneben) plus atomarer Current-Pointer
`C:\Users\jensf\AppData\Local\JFWhisper\backends\cuda\current.json`
(`{"build_id": …, "installed_at_unix": …}`).

- **Versioniert:** ein Build = ein Verzeichnis `\<build_id>\` inkl. `manifest.json`.
  Der zuletzt bestätigte Build bleibt bis zum grünen App-Commit erhalten (Rollback).
- **Build-ID:** `JFW_BUILD_ID = SHA256(kanonisches Produktprofil, kompakt)[:16]` aus
  `product-profiles/jf-whisper.json` (identisch in `build.rs` und
  `release-assets/tools/build_cuda_release.py`). Addon-Manifeste müssen die Build-ID
  der App exakt tragen (`verify_manifest_contract`, fail-closed).
- **Release:** signiert (Minisign, produktiver Addon-Key) unter
  `release-assets\<version>\` (`manifest.json`, `manifest.json.sig`,
  `jf-whisper-server-cuda.tar.gz`, `.sig`). Manifest trägt `alembic_head` und
  `migration_chain_hash` der Migrationskette.
- **Producer-Install (lokal gebautes Release):**
  `python release-assets/tools/install_local_release.py --release-dir … --base-dir … --expect-build-id …`
  — Minisign-Verifikation beider Signaturen, Manifest-Vertrag, Negativinventar,
  Archiv-Hash, per-Datei-SHA-256-Extraktion, atomarer Commit. Nie roh kopieren.
- **Nutzer-Install:** UI „CUDA installieren" (Pipeline `install_release`, Download nur
  aus dem eingebetteten Releasepfad).

## Arbeitsregel: Release-Checkout `_fork` vor JEDEM Build pinnen

Das CUDA-Release wird aus `E:\jf-whisper\_fork` gebaut (eigener Checkout des Forks).
**Vor jedem Release-Build muss `_fork` auf den in `integration/application-lock.json`
gepinnten Gitlink-Stand** (`git fetch origin && git reset --hard origin/foundation/v0.5.0`).
Grund (gemessen 2026-09-24, USCRX-2026-16036): ein veralteter `_fork`-Checkout baute ein
CUDA-Artefakt mit abgeschnittener Alembic-Kette — das im Wechsel starb
(`Unbekannte Schema-Revision 'd8e4c2b1a6f7'`), weil seine Migrationsdateien die
aktuellen Revisionen nicht kannten.

## Betriebsdisziplin (Verträge statt Routinen)

**Deploy-Kette Sidecar (atomar):** bauen (`build_binary.py`) → prüfen (Artefakt-
Gate > 10 KB, `--version`, `_internal` vollständig, Python-DLL vorhanden) →
**ein einziger Tauschschritt**. Keine halben Deploy-Ketten: ein abgebrochener
Kopierschritt hinterlässt ein halb installiertes Artefakt (Vorfall 2026-09-24) —
der Wechselpfad erkennt das inzwischen fail-closed (Sync-Assert Manifest-Build-ID
gegen Build-Verzeichnis, `main.rs start_target`).

**Toolchain-Festlegung (Stand 2026-09-25, gemessen):** CPU-/Sidecar-Builds über
`application/backend/.venv` (Python 3.12.11, PyInstaller 6.22.3); CUDA-Builds
über `_fork/backend/.venv-cuda` (Python 3.11.15, torch 2.11.0+cu128).

- **Mindestversion:** Python 3.11 (darunter tragen weder PyInstaller 6 noch die
  Pins die Werkzeugkette), PyInstaller 6.x.
- **Empfohlen und vertraglich:** genau die oben genannten Venv-Stände. Artefakte
  sind nur reproduzierbar, solange diese Venvs die Werkzeugkette tragen — vor
  jedem Release die Versionen hier nachziehen (`python --version`,
  `python -c "import PyInstaller; print(PyInstaller.__version__)"`).
- **Fallback-Install ist gepinnt:** `scripts/build-server.sh` und das `justfile`
  installieren PyInstaller nur bei Bedarf und dann exakt in Version 6.22.3 —
  ein frisches Venv darf die Werkzeugkette nicht still heben.
- **Python-Version nie still wechseln:** das lauffähige Alt-Artefakt stammt von
  Python 3.11, das CPU-Venv ist inzwischen 3.12 — Mischbauten über Venvs hinweg
  waren der Toolchain-Drift des Vorfalls 2026-09-24.

**Testinstanz-Disziplin (Vorfall 2026-09-24, dreimal betriebsstörend):**
- Eigene Testinstanzen **per PID** beenden (`taskkill /PID <pid> /F`) — nie per
  Wrapperaufruf (der trifft nur den Terminal-Wrapper).
- Nach jedem Lauf einen **Restprozess-Check** fahren (`tasklist | grep -i voicebox`).
- **Teststarts nur ohne aktive Nutzerinstanz** (Datenroot-Lock + Exe-Sperre).
- Fensterlose `voicebox.exe` mit ~0 CPU-Zeit sind Restinstanzen der Testläufe —
  sie blockieren Nutzerstarts (Datenroot-Lock-Zweikampf).

## Artefakt-Gate vor jedem Sidecar-Start

Nur echte Artefakte (> 10 KB) sind startbar (`backend::artifact::is_startable_artifact`);
der ~512-B-Platzhalter von `setup-dev-sidecar.js` ist nicht ausführbar und wird in keiner
Auflösungsstufe gewählt. Im Wechselpfad gilt zusätzlich fail-closed:
`--version` muss zur App-Version passen, sonst Switch abgelehnt (kein stiller Crash).
