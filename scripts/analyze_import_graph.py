#!/usr/bin/env python3
"""JFW-1 Importgraph-Analyse (statisch, AST-basiert, ohne torch).

Baut den Modulgraph des Backends aus allen ``import``/``from ... import``
(inkl. lazy Imports in Funktionen) und berechnet, welche Module vom Entry-Point
(``backend.main``) erreichbar sind. Daraus leitet das Profil-Gate ab, ob verbotene
TTS-/LLM-Pakete (chatterbox, kokoro, qwen_tts, zipvoice, hume, ...) noch aus dem
Laufzeitvertrag erreichbar sind — die Spec verlangt genau diese statischen
Negativtests.

Aufruf:
    python scripts/analyze_import_graph.py                 # Report + Exit 0
    python scripts/analyze_import_graph.py --json          # maschinenlesbar
    python scripts/analyze_import_graph.py --forbidden chatterbox,kokoro
"""

from __future__ import annotations

import argparse
import ast
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
BACKEND = REPO_ROOT / "backend"

# Verbotene Top-Level-Pakete (TTS-/LLM-Engines) laut Produktprofil.
DEFAULT_FORBIDDEN = [
    "chatterbox",
    "kokoro",
    "qwen_tts",
    "zipvoice",
    "hume",
    "parler_tts",
    "f5tts",
]

# Erforderliche Module des Transkriptions-Kerns: diese MUESSEN aus dem
# Entry-Point erreichbar bleiben, sonst ist der STT-Pfad kaputt. Das Gate
# prueft beides — verbotene Pakete verschwinden UND der STT-Kern bleibt.
DEFAULT_REQUIRED = [
    "backend.services.transcribe",   # Whisper-STT
    "backend.routes.transcription",  # Transkriptions-Router
    "backend.routes.captures",       # Captures-Router (STT-Modelle)
]


def module_name_for_file(path: Path) -> tuple[str, bool] | None:
    """backend/foo/bar.py -> ("backend.foo.bar", False); __init__.py -> (Paket, True)."""
    rel = path.relative_to(REPO_ROOT)
    parts = list(rel.with_suffix("").parts)
    is_pkg = parts[-1] == "__init__"
    if is_pkg:
        parts = parts[:-1]
    return ".".join(parts), is_pkg


def _resolve_relative(mod: str, is_pkg: bool, level: int, module_name: str | None) -> str | None:
    """Loest einen relativen Import (level>=1) absolut auf (PEP 366).

    Basis ist das "aktuelle Paket": fuer ein Modul dessen Elternpaket, fuer ein
    __init__-Paket das Paket selbst. level=1 = dieses Paket, jede weitere Stufe
    steigt eine Ebene hoch.
    """
    parts = mod.split(".")
    if is_pkg:
        base_parts = parts[: len(parts) - (level - 1)]
    else:
        # Modul: erst Elternpaket, dann (level-1) Stufen hoch.
        parent = parts[:-1]
        base_parts = parent[: len(parent) - (level - 1)] if level >= 1 else parent
    if not base_parts:
        return None
    if module_name:
        return ".".join(base_parts + [module_name])
    return ".".join(base_parts)


def build_graph() -> dict[str, set[str]]:
    """Modul -> Menge der direkt importierten Module (absolut aufgelöst)."""
    graph: dict[str, set[str]] = {}
    # Venvs/Build-Artefakte sind kein Produktcode — sie duerfen nicht in den
    # Importgraph (sonst bricht der Analyzer an binären Test-Dateien von
    # installierten Paketen).
    skip_dirs = {".venv", "venv", "node_modules", "__pycache__", "build", "dist"}
    for py in BACKEND.rglob("*.py"):
        if any(part in skip_dirs for part in py.parts):
            continue
        parsed = module_name_for_file(py)
        if not parsed:
            continue
        mod, is_pkg = parsed
        try:
            tree = ast.parse(py.read_text(encoding="utf-8"), filename=str(py))
        except SyntaxError:
            continue
        imported: set[str] = set()

        def visit(node, _mod=mod, _is_pkg=is_pkg):
            for child in ast.iter_child_nodes(node):
                if isinstance(child, ast.Import):
                    for a in child.names:
                        imported.add(a.name.split(".")[0])
                elif isinstance(child, ast.ImportFrom):
                    base = None
                    if child.level == 0 and child.module:
                        # absoluter Import: Top-Level-Paket + voller Name merken
                        imported.add(child.module.split(".")[0])
                        base = child.module
                    elif child.level > 0:
                        base = _resolve_relative(_mod, is_pkg, child.level, child.module)
                    if base:
                        imported.add(base)
                        # "from X import Y" kann Y ein Submodul von X sein —
                        # dann wird X.Y geladen. Fuer jedes importierte Name eine
                        # Kante zu <base>.<name> legen (außer Wildcard).
                        for a in child.names:
                            if a.name != "*":
                                imported.add(f"{base}.{a.name}")
                visit(child, _mod)

        visit(tree)
        graph[mod] = imported
    return graph


def reachable_from(graph: dict[str, set[str]], start: str) -> set[str]:
    """Transitive Erreichbarkeit über aufgelöste Importkanten (ohne Spekulation)."""
    seen: set[str] = set()
    stack = [start]
    while stack:
        cur = stack.pop()
        if cur in seen:
            continue
        seen.add(cur)
        for nxt in graph.get(cur, ()):  # type: ignore[arg-type]
            if nxt not in seen:
                stack.append(nxt)
    return seen


def top_level_reachable(reach: set[str]) -> set[str]:
    """Top-Level-Pakete, die aus dem Erreichbarkeits-Satz importiert werden."""
    tops: set[str] = set()
    for mod in reach:
        # nur echte Import-Ziele sammeln (nicht die Backend-Module selbst)
        pass
    return tops


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--forbidden", default=None, help="Kommagetrennte verbotene Top-Level-Pakete")
    ap.add_argument("--required", default=None, help="Kommagetrennte Module, die erreichbar bleiben muessen")
    args = ap.parse_args(argv)

    forbidden = (
        [f.strip() for f in args.forbidden.split(",") if f.strip()]
        if args.forbidden
        else DEFAULT_FORBIDDEN
    )
    required = (
        [r.strip() for r in args.required.split(",") if r.strip()]
        if args.required
        else DEFAULT_REQUIRED
    )

    graph = build_graph()
    entry = "backend.main"
    reach = reachable_from(graph, entry)

    # Welche verbotenen Top-Level-Pakete werden aus erreichbaren Modulen importiert?
    # Ein Import "chatterbox.mtl_tts" trifft auf das verbotene Paket "chatterbox".
    def forbidden_of(name: str) -> str | None:
        top = name.split(".")[0]
        return top if top in forbidden else None

    hits: dict[str, list[str]] = {}
    for mod in sorted(reach):
        for imp in graph.get(mod, ()):  # type: ignore[arg-type]
            f = forbidden_of(imp)
            if f:
                hits.setdefault(f, []).append(mod)

    # Erforderliche Module muessen erreichbar sein (STT-Kern bleibt erhalten).
    missing_required = [m for m in required if m not in reach]

    result = {
        "entry": entry,
        "reachable_modules": len(reach),
        "forbidden_reachable": {k: sorted(v) for k, v in sorted(hits.items())},
        "missing_required": missing_required,
        "clean": not hits and not missing_required,
    }

    if args.json:
        print(json.dumps(result, indent=2))
    else:
        print(f"[import-graph] Entry-Point {entry}: {len(reach)} Module erreichbar")
        if hits:
            print("[import-graph] VERBOTENE Pakete sind aus dem Entry-Point erreichbar:")
            for pkg, mods in sorted(hits.items()):
                print(f"  - {pkg} <- {', '.join(mods[:5])}" + (" …" if len(mods) > 5 else ""))
        else:
            print("[import-graph] OK: keine verbotenen TTS-/LLM-Pakete erreichbar.")
        if missing_required:
            print(f"[import-graph] FEHLER: erforderliche STT-Module nicht erreichbar: {missing_required}")
        elif not hits:
            print("[import-graph] OK: STT-Kern (transcribe/transcription/captures) bleibt erreichbar.")

    return 0 if result["clean"] else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
