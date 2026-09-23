"""JFW-5: kanonische Serialisierung und Hash-Bindung (I/O-frei).

Muster JFW-2/JFW-3/JFW-4/JFW-13: sortierte kanonische JSON-Serialisierung,
UTF-8/LF, genau ein abschliessender Zeilenumbruch fuer Byte-Gleichheit.
"""
from __future__ import annotations

import hashlib
import json

#: Vertragsversion des Batch-Snapshots und der Batch-Identitaet.
CONTRACT_VERSION = "jfw5_batch_v1"


def canonical_json(obj) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def canonical_bytes(obj) -> bytes:
    return (canonical_json(obj) + "\n").encode("utf-8")


def canonical_hash(obj) -> str:
    return hashlib.sha256(canonical_json(obj).encode("utf-8")).hexdigest()
