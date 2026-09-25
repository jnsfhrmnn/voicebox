# Build- und Artefakt-Vertrag (jf-whisper / voicebox-Fork)

Gilt für `application/` (Submodule, Fork `jnsfhrmnn/voicebox`) und die Release-Werkzeuge
unter `E:\jf-whisper\release-assets\tools\`. Änderungen an der Werkzeugkette nur hier
dokumentiert einführen — reproduzierbare Artefakte sind Vertrag, keine Routine.

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

**Toolchain-Festlegung (Stand 2026-09-24):** CPU-/Sidecar-Builds über
`application/backend/.venv` (Python 3.12.11, PyInstaller 6.22.3); CUDA-Builds
über `_fork/backend/.venv-cuda` (Python 3.11.15, torch 2.11.0+cu128). Artefakte
sind nur reproduzierbar, solange diese Venvs die Werkzeugkette tragen — vor
jedem Release die Versionen hier nachziehen.

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
