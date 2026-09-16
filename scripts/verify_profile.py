#!/usr/bin/env python3
"""JFW-1 Produktprofil-Gate: rechnet den Profilhash und prueft den Fork fail-closed.

Das Transkriptions-Produktprofil (product-profiles/jf-whisper.json) ist die einzige
Build-Wahrheit fuer aktive/verbotene Frontend-Routen, FastAPI-Router, Modellklassen,
Sidecars und Tauri-Capabilities. Dieses Script:

  --check        prueft den Fork gegen das Profil (fail-closed, Exit != 0 bei Drift)
  --write-marker schreibt generated/profile-hash.txt (Profilhash + Baseline + Ledger)

Der Marker wird versioniert; Build und Test vergleichen ihn gegen das aktuelle Profil.
Drift blockiert — ein verbotener Router oder eine falsche App-Identitaet darf keinen
Release bauen.

Exit-Codes: 0 = ok · 1 = drift/fehlende Pflichtstruktur · 2 = Profil unlesbar
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PROFILE_PATH = ROOT / "product-profiles" / "jf-whisper.json"
MARKER_PATH = ROOT / "generated" / "profile-hash.txt"
ROUTES_INIT = ROOT / "backend" / "routes" / "__init__.py"
TAURI_CONF = ROOT / "tauri" / "src-tauri" / "tauri.conf.json"


def profile_hash(profile: dict) -> str:
    canon = json.dumps(profile, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(canon.encode("utf-8")).hexdigest()


def load_profile() -> dict:
    if not PROFILE_PATH.exists():
        print(f"[profile-gate] FEHLT: {PROFILE_PATH}", file=sys.stderr)
        sys.exit(2)
    try:
        return json.loads(PROFILE_PATH.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        print(f"[profile-gate] Profil unlesbar: {e}", file=sys.stderr)
        sys.exit(2)


def check_routers(profile: dict, errors: list[str]) -> None:
    """Fail-closed Importgraph-Check gegen backend/routes/__init__.py."""
    if not ROUTES_INIT.exists():
        errors.append(f"fehlende Datei: {ROUTES_INIT}")
        return
    src = ROUTES_INIT.read_text(encoding="utf-8")
    included = set(re.findall(r"app\.include_router\((\w+)_router\)", src))
    active = set(profile["fastapi_routers"]["active"])
    forbidden = set(profile["fastapi_routers"]["forbidden"])

    missing_active = sorted(active - included)
    leaked_forbidden = sorted(forbidden & included)
    unknown = sorted(included - active - forbidden)

    if missing_active:
        errors.append(f"aktive Router fehlen im Importgraph: {missing_active}")
    if leaked_forbidden:
        errors.append(f"VERBOTENE Router aktiv im Importgraph: {leaked_forbidden}")
    if unknown:
        errors.append(f"Router weder aktiv noch verboten (Profil unvollstaendig): {unknown}")


def check_tauri(profile: dict, errors: list[str]) -> None:
    """Fail-closed App-Identitaets- und Capability-Check gegen tauri.conf.json."""
    if not TAURI_CONF.exists():
        errors.append(f"fehlende Datei: {TAURI_CONF}")
        return
    conf = json.loads(TAURI_CONF.read_text(encoding="utf-8"))

    ident = profile["app_identity"]
    if conf.get("identifier") != ident["tauri_identifier"]:
        errors.append(
            f"Tauri-Identifier {conf.get('identifier')!r} != Profil {ident['tauri_identifier']!r}"
        )
    if conf.get("productName") != ident["product_name"]:
        errors.append(f"productName {conf.get('productName')!r} != Profil {ident['product_name']!r}")

    app = conf.get("app", {})
    caps = profile["tauri_capabilities"]
    if app.get("withGlobalTauri") is not caps["with_global_tauri"]:
        errors.append(f"withGlobalTauri={app.get('withGlobalTauri')} != Profil {caps['with_global_tauri']}")

    plugins = conf.get("plugins", {})
    if caps["updater_enabled"] and "updater" not in plugins:
        errors.append("Profil erwartet Updater, aber tauri.conf.json hat keinen updater-Block")
    if not caps["updater_enabled"] and "updater" in plugins:
        errors.append("UPDATER AKTIV — Profil verbietet den Voicebox-Upstream-Updatekanal")

    shell = plugins.get("shell", {})
    # JFW-1: Tauri erwartet hier ein Schema-gültiges Feld — die Allowlist ist im
    # Fork deaktiviert ("open": false). Beide Formen sind erlaubt; nur eine
    # nicht-leere, vom Profil abweichende Allowlist ist Drift.
    open_cfg = shell.get("open", [])
    allowlist = set(open_cfg) if isinstance(open_cfg, list) else set()
    if allowlist != set(caps["shell_open_allowlist"]):
        errors.append(f"Shell-Allowlist {sorted(allowlist)} != Profil {caps['shell_open_allowlist']}")

    security = app.get("security", {})
    csp = security.get("csp")
    if caps["csp"] and not (isinstance(csp, str) and "default-src 'self'" in csp):
        errors.append(f"CSP {csp!r} ist nicht restriktiv genug (Profil: gebuendelte Ressourcen + Tauri-IPC)")


def check_frontend_routes(profile: dict, errors: list[str]) -> None:
    """Statische Route-Negativpruefung gegen app/src/router.tsx."""
    router_tsx = ROOT / "app" / "src" / "router.tsx"
    if not router_tsx.exists():
        errors.append(f"fehlende Datei: {router_tsx}")
        return
    src = router_tsx.read_text(encoding="utf-8")
    declared = set(re.findall(r'path:\s*"(/[^"]*)"', src))
    forbidden = set(profile["frontend_routes"]["forbidden"])
    leaked = sorted(forbidden & declared)
    if leaked:
        errors.append(f"VERBOTENE Frontend-Routen deklariert in router.tsx: {leaked}")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--check", action="store_true", help="Fork gegen Profil pruefen (fail-closed)")
    ap.add_argument("--write-marker", action="store_true", help="generated/profile-hash.txt schreiben")
    ap.add_argument("--schema", metavar="DB", default=None,
                    help="zusaezlich: jf-whisper-DB gegen database_tables des Profils pruefen (fail-closed)")
    args = ap.parse_args()

    profile = load_profile()
    phash = profile_hash(profile)

    if args.write_marker:
        MARKER_PATH.parent.mkdir(parents=True, exist_ok=True)
        marker = {
            "profile": profile["profile"],
            "version": profile["version"],
            "profile_sha256": phash,
            "baseline_commit": profile["baseline_commit"],
            "patch_ledger": profile["patch_ledger"],
        }
        MARKER_PATH.write_text(
            json.dumps(marker, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
        print(f"[profile-gate] Marker geschrieben: {MARKER_PATH} (sha256={phash[:16]}…)")

    if args.check:
        errors: list[str] = []
        check_routers(profile, errors)
        check_tauri(profile, errors)
        check_frontend_routes(profile, errors)

        # Optionales Schema-Gate: DB gegen database_tables des Profils pruefen.
        if args.schema:
            from verify_schema import check as schema_check  # noqa: PLC0415 (gleicher Ordner)
            rc = schema_check(Path(args.schema))
            if rc != 0:
                errors.append(f"Schema-Gate fehlgeschlagen (Exit {rc}): {args.schema}")

        # Marker-Drift: versionierter Hash muss zum aktuellen Profil passen
        if MARKER_PATH.exists():
            marker = json.loads(MARKER_PATH.read_text(encoding="utf-8"))
            if marker.get("profile_sha256") != phash:
                errors.append(
                    f"Marker-Drift: {MARKER_PATH.name} haelt {str(marker.get('profile_sha256'))[:16]}… , "
                    f"Profil ist {phash[:16]}… — Marker neu schreiben (--write-marker) oder Profil zuruecksetzen"
                )
        else:
            errors.append(f"Marker fehlt: {MARKER_PATH} — erst --write-marker ausfuehren")

        if errors:
            print("[profile-gate] DRIFT (fail-closed):", file=sys.stderr)
            for e in errors:
                print(f"  - {e}", file=sys.stderr)
            return 1
        print(f"[profile-gate] OK: Profil {profile['profile']} v{profile['version']} "
              f"(sha256={phash[:16]}…) — Importgraph, Tauri-Identitaet und Routen konsistent")

    if not args.check and not args.write_marker:
        print(f"[profile-gate] Profil {profile['profile']} v{profile['version']} sha256={phash}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
