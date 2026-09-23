"""JFW-5: Identitaet — Pfade, Inhaltsnachweis, Batch-/Element-/Versuch-IDs (I/O-frei).

Pfadkanonisierung ueber ``ntpath.normpath``/``abspath`` + ``normcase`` —
bewusst OHNE ``realpath``: Reparse Points, Symlinks und Junctions werden nie
automatisch gefolgt (Spec-Ausschlussregel). Der Inhaltsnachweis
``content_proof_v1`` ist der vollstaendige SHA-256 ueber alle Bytes (stromweise
ueber einen Chunk-Iterator) — die einzige Variante, die den Commit-Bindungs-AC
an die vollstaendig konsumierten Bytes exakt erfuellt.
"""
from __future__ import annotations

import hashlib
import ntpath
import os
import uuid
from collections.abc import Iterable

from .provenance import canonical_hash, canonical_json

ITEM_ID_PREFIX = "jfw5-item-"
BATCH_ID_PREFIX = "jfw5-batch-"
ATTEMPT_ID_PREFIX = "jfw5-attempt-"
CONTENT_PROOF_VERSION = "content_proof_v1"


def canonical_path(path: str) -> str:
    """Normalisierter absoluter Pfad — ohne ``realpath`` (folgt keinem Link)."""
    p = ntpath.normpath(str(path))
    if not ntpath.isabs(p):
        p = ntpath.normpath(ntpath.join(os.getcwd(), p))
    return p


def path_identity_key(path: str) -> str:
    """Identitaetsschluessel eines Pfades (Gross/Klein egal auf Windows)."""
    return ntpath.normcase(canonical_path(path))


def is_unc(path: str) -> bool:
    return ntpath.splitdrive(canonical_path(path))[0].startswith("\\\\")


def content_proof(chunks: Iterable[bytes]) -> str:
    """``content_proof_v1``: ``sha256:<hex>`` ueber die vollstaendigen Bytes."""
    h = hashlib.sha256()
    for chunk in chunks:
        h.update(chunk)
    return f"sha256:{h.hexdigest()}"


def source_identity(source: dict) -> dict:
    """Kanonische Quellenidentitaet (Pfad + Groesse + Merkmal + Inhaltsnachweis)."""
    return {
        "path_key": path_identity_key(source["path"]),
        "size": int(source["size"]),
        "mtime_ns": int(source["mtime_ns"]),
        "content_proof": str(source["content_proof"]),
    }


def source_identity_hash(source: dict) -> str:
    return canonical_hash(source_identity(source))


def element_id(source: dict) -> str:
    """Stabile, deterministische Element-ID aus der kanonischen Quellenidentitaet.

    Bewusst pfadbasiert: dieselbe Quelle unter derselben Pfadidentitaet behaelt
    ihre Element-ID; eine Quellenaenderung wird ueber den Inhaltsnachweis als
    ``invalidated`` sichtbar und nie als stille neue Identitaet verarbeitet.
    """
    digest = canonical_hash({"path_key": path_identity_key(source["path"])})
    return ITEM_ID_PREFIX + digest[:16]


def new_batch_id() -> str:
    return BATCH_ID_PREFIX + uuid.uuid4().hex


def new_attempt_id() -> str:
    return ATTEMPT_ID_PREFIX + uuid.uuid4().hex


__all__ = [
    "ATTEMPT_ID_PREFIX",
    "BATCH_ID_PREFIX",
    "CONTENT_PROOF_VERSION",
    "ITEM_ID_PREFIX",
    "canonical_json",
    "canonical_path",
    "content_proof",
    "element_id",
    "is_unc",
    "new_attempt_id",
    "new_batch_id",
    "path_identity_key",
    "source_identity",
    "source_identity_hash",
]
