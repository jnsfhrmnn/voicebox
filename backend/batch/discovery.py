"""JFW-5: Discovery — Fundmenge, Ausschluesse, Deduplizierung (I/O-frei ueber ``DiscoveryFs``).

Dokumentierte Fundregel: Ordnerquellen werden rekursiv durchlaufen (Eintraege
nach Namen sortiert, Gross/Klein egal); ausschliesslich unterstuetzte lokale
Mediendateien werden Element. Versteckte/System-Eintraege sowie Reparse
Points/Symlinks/Junctions werden weder durchlaufen noch gefolgt — ihr Ausschluss
erscheint mit konkretem Grund in der Vorschau. Dieselbe physische Quelle (gleiche
Pfadidentitaet) wird genau einmal Element und bewahrt alle Auswahlreferenzen;
identische Inhaltsbytes getrennter Pfade bleiben getrennte Elemente mit
sichtbarem Duplikathinweis.
"""
from __future__ import annotations

import ntpath

from .identity import (
    canonical_path,
    content_proof,
    element_id,
    is_unc,
    path_identity_key,
)

#: Dokumentierte Unterstuetzungsregel (Audio- und Audio-Video-Container).
SUPPORTED_MEDIA_EXTENSIONS = (
    ".wav", ".mp3", ".m4a", ".flac", ".ogg", ".opus", ".aac", ".wma",
    ".mp4", ".m4v", ".mkv", ".mka", ".webm", ".mov", ".avi",
)

EXCLUSION_REASONS = (
    "pfad_fehlt",
    "nicht_lesbar",
    "format_nicht_unterstuetzt",
    "versteckt_oder_system",
    "reparse_point_nicht_gefolgt",
    "auswahl_unbekannt",
)


def _join(root: str, name: str) -> str:
    """Display-Pfad im Trennzeichenstil des Wurzelpfades (Vorschau-Treue)."""
    sep = "\\" if (root.startswith("\\\\") or "\\" in root) else "/"
    return root.rstrip("/\\") + sep + name


def _supported(name: str) -> bool:
    return ntpath.splitext(name)[1].lower() in SUPPORTED_MEDIA_EXTENSIONS


def discover(selection: list[dict], fs) -> dict:
    """Bildet die Fundmenge gegen die injizierbare Dateisystem-Probe ``fs``.

    ``fs`` liefert ``list_dir`` (Eintrags-Dicts), ``file_facts`` (size,
    mtime_ns) und ``read_chunks`` (Bytes-Chunks). ``FileNotFoundError`` => Pfad
    fehlt, ``PermissionError``/``OSError`` => nicht lesbar.
    """
    elements: dict[str, dict] = {}
    excluded: list[dict] = []
    selection_refs: list[str] = []
    trust_boundary: list[str] = []

    def note_trust(path: str) -> None:
        if is_unc(path) and path not in trust_boundary:
            trust_boundary.append(path)

    def add_file(display: str, ref: str) -> None:
        key = path_identity_key(display)
        if key in elements:
            elements[key]["selection_refs"].append(ref)
            return
        canon = canonical_path(display)
        try:
            facts = fs.file_facts(canon)
        except FileNotFoundError:
            excluded.append({"path": display, "reason_code": "pfad_fehlt"})
            return
        except (PermissionError, OSError):
            excluded.append({"path": display, "reason_code": "nicht_lesbar"})
            return
        try:
            proof = content_proof(fs.read_chunks(canon))
        except FileNotFoundError:
            excluded.append({"path": display, "reason_code": "pfad_fehlt"})
            return
        except (PermissionError, OSError):
            excluded.append({"path": display, "reason_code": "nicht_lesbar"})
            return
        hints = []
        if int(facts.get("size") or 0) == 0:
            hints.append("leere_datei")
        source = {
            "path": display,
            "size": int(facts.get("size") or 0),
            "mtime_ns": int(facts.get("mtime_ns") or 0),
            "content_proof": proof,
        }
        elements[key] = {
            "item_id": element_id(source),
            "path": display,
            "path_key": key,
            "source": source,
            "selection_refs": [ref],
            "hints": hints,
            "duplicate_hint": False,
        }
        note_trust(display)

    def walk_folder(root: str, folder: str) -> None:
        try:
            entries = fs.list_dir(canonical_path(folder))
        except FileNotFoundError:
            excluded.append({"path": folder, "reason_code": "pfad_fehlt"})
            return
        except (PermissionError, OSError):
            excluded.append({"path": folder, "reason_code": "nicht_lesbar"})
            return
        for entry in sorted(entries, key=lambda e: e["name"].casefold()):
            name = entry["name"]
            full = _join(folder, name)
            if entry.get("hidden") or entry.get("system"):
                excluded.append({"path": full, "reason_code": "versteckt_oder_system"})
                continue
            if entry.get("is_reparse"):
                excluded.append({"path": full, "reason_code": "reparse_point_nicht_gefolgt"})
                continue
            if entry.get("is_dir"):
                walk_folder(root, full)
                continue
            if entry.get("is_file"):
                if _supported(name):
                    rel = ntpath.relpath(canonical_path(full), canonical_path(root))
                    add_file(full, root)
                    key = path_identity_key(full)
                    if key in elements:
                        elements[key]["relative_path"] = rel.replace("\\", "/")
                else:
                    excluded.append({"path": full, "reason_code": "format_nicht_unterstuetzt"})

    for ref in selection:
        kind = ref.get("kind")
        path = str(ref.get("path") or "")
        selection_refs.append(path)
        note_trust(path)
        if kind == "file":
            add_file(path, path)
        elif kind == "folder":
            walk_folder(path, path)
        else:
            excluded.append({"path": path, "reason_code": "auswahl_unbekannt"})

    # Relativpfad fuer rein ausgewaehlte Einzeldateien: Dateiname.
    ordered = sorted(elements.values(), key=lambda e: e["path_key"])
    for el in ordered:
        el.setdefault("relative_path", ntpath.basename(el["path"]).replace("\\", "/"))

    # Duplikathinweise: identische Inhaltsbytes getrennter Pfade bleiben getrennt.
    by_proof: dict[str, list[dict]] = {}
    for el in ordered:
        by_proof.setdefault(el["source"]["content_proof"], []).append(el)
    duplicate_groups = []
    for _proof, group in sorted(by_proof.items()):
        if len(group) > 1:
            for el in group:
                el["duplicate_hint"] = True
            duplicate_groups.append([el["item_id"] for el in group])

    return {
        "selection": selection_refs,
        "elements": ordered,
        "excluded": excluded,
        "duplicate_groups": duplicate_groups,
        "counts": {"included": len(ordered), "excluded": len(excluded)},
        "total_size": sum(el["source"]["size"] for el in ordered),
        "trust_boundary_paths": trust_boundary,
    }
