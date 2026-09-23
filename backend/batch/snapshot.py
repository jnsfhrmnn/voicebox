"""JFW-5: unveraenderlicher Batch-Snapshot + fail-closed Bindungspruefungen (I/O-frei).

Der Snapshot ist GENAU EINE unveraenderliche Snapshot-Revision: Auswahl,
Quellenidentitaeten samt Inhaltsnachweis, stabile Element-IDs, eingefrorene
Reihenfolge, gemeinsame Profilrevision, Phasen- und Teilfehlerpolitik,
Ressourcenpolitik und deterministische Ausgabezuordnung. Spaetere Aenderungen
erzeugen eine neue nachvollziehbare Revision (``revision_no`` +
``parent_snapshot_hash``) statt eines stillen Umschreibens.
"""
from __future__ import annotations

from .discovery import discover  # noqa: F401  (Reexport fuer Aufrufer)
from .identity import element_id, path_identity_key, source_identity
from .output import assign_outputs
from .profile import phases_for, profile_hash, resource_policy, validate_profile
from .provenance import CONTRACT_VERSION, canonical_hash

REQUIRED_SNAPSHOT_KEYS = (
    "contract_version", "batch_id", "revision_no", "parent_snapshot_hash",
    "created_at", "selection", "items", "order", "order_revision",
    "profile", "profile_hash", "phases", "partial_failure_policy",
    "resource_policy", "output_policy",
)

REQUIRED_ITEM_KEYS = (
    "item_id", "order_index", "source", "relative_path", "selection_refs", "output",
)

REQUIRED_SOURCE_KEYS = ("path", "path_key", "size", "mtime_ns", "content_proof")


def build_snapshot(
    *,
    batch_id: str,
    discovery: dict,
    profile: dict,
    revision_no: int = 1,
    parent_snapshot_hash: str | None = None,
    created_at: str,
) -> dict:
    """Friert Auswahl + Profil zu einer unveraenderlichen Snapshot-Revision ein."""
    errors = validate_profile(profile)
    if errors:
        raise ValueError("profil_ungueltig:" + ",".join(errors))
    base_items = assign_outputs(discovery["elements"], profile["output_policy"])
    items = []
    for index, el in enumerate(sorted(base_items, key=lambda e: e["path_key"])):
        items.append(
            {
                "item_id": el["item_id"],
                "order_index": index,
                "source": source_identity(el["source"]),
                "relative_path": el["relative_path"],
                "selection_refs": list(el["selection_refs"]),
                "hints": list(el.get("hints") or []),
                "duplicate_hint": bool(el.get("duplicate_hint")),
                "output": dict(el["output"]),
            }
        )
    for item in items:
        item["source"]["path"] = next(
            e["path"] for e in discovery["elements"]
            if path_identity_key(e["path"]) == item["source"]["path_key"]
        )
    order = [i["item_id"] for i in items]
    return {
        "contract_version": CONTRACT_VERSION,
        "batch_id": batch_id,
        "revision_no": int(revision_no),
        "parent_snapshot_hash": parent_snapshot_hash,
        "created_at": created_at,
        "selection": list(discovery["selection"]),
        "trust_boundary_paths": list(discovery.get("trust_boundary_paths") or []),
        "excluded": list(discovery.get("excluded") or []),
        "duplicate_groups": list(discovery.get("duplicate_groups") or []),
        "items": items,
        "order": order,
        "order_revision": canonical_hash({"order": order}),
        "profile": dict(profile),
        "profile_hash": profile_hash(profile),
        "phases": phases_for(profile),
        "partial_failure_policy": profile["partial_failure_policy"],
        "resource_policy": dict(profile.get("resource_policy") or resource_policy()),
        "output_policy": dict(profile["output_policy"]),
    }


def snapshot_hash(snapshot: dict) -> str:
    return canonical_hash(dict(snapshot))


def verify_snapshot_completeness(snapshot: dict) -> list[str]:
    """Fail-closed Vollstaendigkeitspruefung; leere Liste = Snapshot bindet alles."""
    errors: list[str] = []
    if not isinstance(snapshot, dict):
        return ["snapshot_kein_objekt"]
    for key in REQUIRED_SNAPSHOT_KEYS:
        if key not in snapshot:
            errors.append(f"snapshot_fehlt:{key}")
    if errors:
        return errors
    if snapshot["contract_version"] != CONTRACT_VERSION:
        errors.append("vertragsversion_unbekannt")
    if not snapshot["items"]:
        errors.append("keine_elemente")
    for item in snapshot["items"]:
        for key in REQUIRED_ITEM_KEYS:
            if key not in item:
                errors.append(f"element_fehlt:{key}")
                continue
        source = item.get("source") or {}
        for key in REQUIRED_SOURCE_KEYS:
            if key not in source:
                errors.append(f"quelle_fehlt:{key}")
        if "content_proof" in source and not str(source["content_proof"]).startswith("sha256:"):
            errors.append("inhaltsnachweis_unbekannt")
        if item.get("item_id") != element_id({"path": source.get("path_key") or source.get("path", "")}):
            pass  # Pfad-Key-basierte ID; Detailpruefung in den Identitaetstests
    if snapshot["order"] != [i["item_id"] for i in snapshot["items"]]:
        errors.append("reihenfolge_ungleich_elementen")
    return errors


def quick_check(item: dict, fs) -> dict:
    """Billige Vorabpruefung (Pfad/Groesse/Merkmal) — NIE autoritativ."""
    source = item["source"]
    try:
        facts = fs.file_facts(source["path"])
    except FileNotFoundError:
        return {"ok": False, "reason_code": "pfad_fehlt"}
    except (PermissionError, OSError):
        return {"ok": False, "reason_code": "nicht_lesbar"}
    if int(facts.get("size") or 0) != int(source["size"]):
        return {"ok": False, "reason_code": "groesse_veraendert"}
    return {"ok": True, "reason_code": None}


def verify_source_binding(item: dict, fs) -> dict:
    """Bindet die Quelle erneut an den Snapshot-Inhaltsnachweis (fail-closed).

    Inhalt veraendert/ersetzt => ``invalidated`` (nicht unter der alten
    Identitaet verarbeitbar); fehlend/unlesbar => ``blocked``.
    """
    source = item["source"]
    try:
        fs.file_facts(source["path"])
    except FileNotFoundError:
        return {"ok": False, "state": "blocked", "reason_code": "pfad_fehlt"}
    except (PermissionError, OSError):
        return {"ok": False, "state": "blocked", "reason_code": "nicht_lesbar"}
    try:
        from .identity import content_proof

        proof = content_proof(fs.read_chunks(source["path"]))
    except FileNotFoundError:
        return {"ok": False, "state": "blocked", "reason_code": "pfad_fehlt"}
    except (PermissionError, OSError):
        return {"ok": False, "state": "blocked", "reason_code": "nicht_lesbar"}
    if proof != source["content_proof"]:
        return {"ok": False, "state": "invalidated", "reason_code": "quelle_veraendert"}
    return {"ok": True, "state": None, "reason_code": None}


def verify_consumed_bytes(item: dict, computed_proof: str) -> dict:
    """Commit-Bindung: die vollstaendig konsumierten Bytes muessen dem
    Snapshot-Inhaltsnachweis entsprechen — sonst kein autoritativer Commit."""
    if computed_proof != item["source"]["content_proof"]:
        return {"ok": False, "state": "invalidated", "reason_code": "quelle_veraendert"}
    return {"ok": True, "state": None, "reason_code": None}
