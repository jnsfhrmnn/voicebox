#!/usr/bin/env python3
"""Atomare Deploy-Kette für das Sidecar (USCRX-2026-16106 / USCRX-2026-16038).

Vertrag siehe BUILD.md: bauen (backend/build_binary.py — der EINZIGE
zulässige Build-Weg) → prüfen (Artefakt-Gate, `--version`, `_internal`
vollständig, Python-DLL vorhanden) → **ein einziger Tauschschritt** →
Deploy-Check. Keine halben Deploy-Ketten: ein abgebrochener Kopierschritt
hinterlässt ein halb installiertes Artefakt (Vorfall 2026-09-24).

Usage:
    python scripts/deploy_sidecar.py --target-dir <App-Exe-Dir>
    python scripts/deploy_sidecar.py --target-dir <App-Exe-Dir> --cuda
    python scripts/deploy_sidecar.py --target-dir <App-Exe-Dir> --skip-build
    python scripts/deploy_sidecar.py --check --target-dir <App-Exe-Dir>

Exit-Codes: 0 = ok · 2 = Gate fehlgeschlagen · 3 = Tausch fehlgeschlagen
(zurückgerollt) · 4 = Fehler in der Kette (Build/Quelle).
"""

import argparse
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

# Artefakt-Gate (BUILD.md): nur echte Artefakte sind startbar — der ~512-B-
# Platzhalter von setup-dev-sidecar.js fällt hier ebenso heraus wie halbe
# Kopien. Die Grenze deckt sich mit backend::artifact::is_startable_artifact.
MIN_ARTIFACT_BYTES = 10 * 1024
VERSION_TIMEOUT_S = 120


def log(msg: str) -> None:
    print(f"[DEPLOY] {msg}", flush=True)


def binary_name(cuda: bool) -> str:
    return "jf-whisper-server-cuda" if cuda else "jf-whisper-server"


def artifact_exe(root: Path, name: str) -> Path:
    """Pfad zum startbaren Exe innerhalb eines Artefakt-Layouts (onedir/onefile)."""
    onedir = root / name / (f"{name}.exe" if os.name == "nt" else name)
    if onedir.is_file():
        return onedir
    onefile = root / (f"{name}.exe" if os.name == "nt" else name)
    return onefile


def find_python_dll(artifact_root: Path, name: str) -> Path | None:
    """Python-DLL im Artefakt suchen (onedir: _internal/, onefile: daneben nicht prüfbar)."""
    internal = artifact_root / name / "_internal"
    if not internal.is_dir():
        internal = artifact_root / "_internal"
    if not internal.is_dir():
        return None
    for pattern in ("python3*.dll", "libpython3*.so*", "libpython3*.dylib"):
        hits = sorted(internal.glob(pattern))
        if hits:
            return hits[0]
    return None


def check_artifact(artifact_root: Path, name: str, expect_version: str | None) -> list[tuple[str, bool, str]]:
    """Deploy-Check / Artefakt-Gate. Liefert (Check, ok, Detail) je Prüfpunkt."""
    results: list[tuple[str, bool, str]] = []
    exe = artifact_exe(artifact_root, name)

    ok = exe.is_file()
    results.append(("exe-vorhanden", ok, str(exe) if ok else f"fehlt: {exe}"))
    if not ok:
        return results

    size = exe.stat().st_size
    ok = size > MIN_ARTIFACT_BYTES
    results.append(("artefakt-gate", ok, f"{size} Bytes (Grenze {MIN_ARTIFACT_BYTES})"))

    onedir_dir = exe.parent if exe.parent.name == name else None
    if onedir_dir is not None:
        internal = onedir_dir / "_internal"
        ok = internal.is_dir() and any(internal.iterdir())
        n = len(list(internal.iterdir())) if internal.is_dir() else 0
        results.append(("_internal-vollstaendig", ok, f"{n} Einträge in {internal}"))
    else:
        results.append(("_internal-vollstaendig", True, "onefile-Layout — kein _internal erwartet"))

    dll = find_python_dll(artifact_root, name)
    if onedir_dir is None:
        # onefile: die Python-Laufzeit ist ins Exe gebündelt, eine _internal-
        # DLL darf hier nie erwartet werden (sonst Fehlalarm am Gate).
        results.append(("python-dll", True, "onefile-Layout — Python-Laufzeit ist im Artefakt gebündelt"))
    else:
        results.append(("python-dll", dll is not None, str(dll) if dll else "keine python3*-DLL im Artefakt gefunden"))

    try:
        proc = subprocess.run(
            [str(exe), "--version"],
            capture_output=True,
            text=True,
            timeout=VERSION_TIMEOUT_S,
            cwd=str(exe.parent),
        )
        out = (proc.stdout or "").strip()
        ok = proc.returncode == 0 and out.startswith(name.split("-cuda")[0])
        results.append(("version-ausfuehrbar", ok, f"exit={proc.returncode} stdout={out!r}"))
        if expect_version is not None:
            ok = expect_version in out
            results.append(("version-erwartung", ok, f"erwartet {expect_version!r} in {out!r}"))
    except Exception as exc:  # noqa: BLE001 — jedes Versagen ist ein Gate-Befund
        results.append(("version-ausfuehrbar", False, f"{type(exc).__name__}: {exc}"))

    return results


def gate_ok(results: list[tuple[str, bool, str]]) -> bool:
    for name_, ok, detail in results:
        log(f"{'OK ' if ok else 'FAIL'} {name_}: {detail}")
    return all(ok for _, ok, _ in results)


def build(cuda: bool) -> None:
    backend_dir = Path(__file__).resolve().parent.parent / "backend"
    builder = backend_dir / "build_binary.py"
    cmd = [sys.executable, str(builder)]
    if cuda:
        cmd.append("--cuda")
    log(f"bau: {' '.join(cmd)}")
    proc = subprocess.run(cmd, cwd=str(backend_dir))
    if proc.returncode != 0:
        raise SystemExit(f"[DEPLOY] Build fehlgeschlagen (exit={proc.returncode})")


def swap_atomically(target: Path, staged: Path) -> Path | None:
    """Ein einziger Tauschschritt: Altstand wegstellen, Neustand umbenennen.

    Beide Umbenennungen liegen auf demselben Volume (staged wird direkt neben
    dem Ziel abgelegt) und sind einzeln atomar. Schlägt der zweite Schritt
    fehl, rollt der erste zurück — es gibt nie ein halb installiertes Ziel.
    """
    backup = None
    if target.exists():
        backup = target.with_name(f"{target.name}.old-{time.strftime('%Y%m%d-%H%M%S')}")
        os.replace(target, backup)
    try:
        os.replace(staged, target)
    except Exception:
        if backup is not None and backup.exists() and not target.exists():
            os.replace(backup, target)
            log(f"tausch fehlgeschlagen — Altstand zurückgerollt: {target}")
        raise
    return backup


def cleanup(target: Path, name: str, keep: int) -> None:
    parent = target.parent
    for stale in sorted(parent.glob(f".{name}.deploy-staging-*")):
        shutil.rmtree(stale, ignore_errors=True)
        log(f"staging entfernt: {stale}")
    olds = sorted(parent.glob(f"{name}.old-*"))
    for old in olds[:-keep] if keep > 0 else olds:
        if old.is_dir():
            shutil.rmtree(old, ignore_errors=True)
        else:
            old.unlink(missing_ok=True)
        log(f"altstand entfernt: {old}")


def deploy(args: argparse.Namespace) -> int:
    name = binary_name(args.cuda)
    backend_dir = Path(__file__).resolve().parent.parent / "backend"
    dist = backend_dir / "dist"

    if not args.skip_build:
        build(args.cuda)

    source_root = dist
    exe = artifact_exe(source_root, name)
    if not exe.is_file():
        log(f"FAIL quellartefakt fehlt: {exe}")
        return 4

    log(f"quelle: {exe}")
    if not gate_ok(check_artifact(source_root, name, args.expect_version)):
        log("FAIL quellartefakt hat das Gate nicht bestanden — kein Tausch")
        return 2

    target_dir = Path(args.target_dir).resolve()
    if not target_dir.is_dir():
        log(f"FAIL target-dir existiert nicht: {target_dir}")
        return 4
    target = target_dir / name if exe.parent.name == name else target_dir / exe.name

    staged = target_dir / f".{name}.deploy-staging-{time.strftime('%Y%m%d-%H%M%S')}"
    log(f"staging: {staged}")
    if exe.parent.name == name:
        shutil.copytree(exe.parent, staged)
    else:
        staged.mkdir(parents=True)
        shutil.copy2(exe, staged / exe.name)

    try:
        backup = swap_atomically(target, staged)
    except Exception as exc:  # noqa: BLE001
        log(f"FAIL tausch: {type(exc).__name__}: {exc}")
        return 3
    log(f"getauscht: {target}" + (f" (Altstand: {backup})" if backup else ""))

    if not gate_ok(check_artifact(target_dir, name, args.expect_version)):
        log("FAIL deploy-check nach dem Tausch — Ziel bitte prüfen, Altstand liegt daneben")
        return 2

    cleanup(target, name, args.keep)
    log("fertig: deploy-check bestanden")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Atomare Sidecar-Deploy-Kette (BUILD.md)")
    parser.add_argument("--target-dir", required=True, help="Verzeichnis der App-Exe (dort löst die App die Sidecar auf)")
    parser.add_argument("--cuda", action="store_true", help="CUDA-Artefakt statt CPU")
    parser.add_argument("--skip-build", action="store_true", help="vorhandenes dist/-Artefakt deployen, nicht neu bauen")
    parser.add_argument("--expect-version", help="erwartete Version im --version-Output (optional)")
    parser.add_argument("--check", action="store_true", help="nur Deploy-Check am Ziel, kein Bauen/Tauschen")
    parser.add_argument("--keep", type=int, default=1, help="Anzahl aufbewahrter Altstände neben dem Ziel (default 1)")
    args = parser.parse_args()

    name = binary_name(args.cuda)
    if args.check:
        target_dir = Path(args.target_dir).resolve()
        results = check_artifact(target_dir, name, args.expect_version)
        return 0 if gate_ok(results) else 2

    return deploy(args)


if __name__ == "__main__":
    sys.exit(main())
