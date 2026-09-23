"""JFW-4: eingefrorene Gold-Bytes — Byte-Identitaets-Abnahme-Gate.

Die SHA-256-Hashes der fachlichen Set-Inhalte (JSON/SRT/VTT) je Fixture sind in
``fixtures/jfw4/golden_hashes.json`` eingefroren. Abweichungen bedeuten eine
Vertragsaenderung und duerfen nicht still passieren (Schema-Evolution nur ueber
neue Vertragsversionen). Zusaetzlich wird die Byte-Identitaet ueber drei Läufe
gemessen.

Ausfuehrung:  backend/.venv/Scripts/python.exe -m pytest backend/tests/test_export_golden.py
"""
import hashlib
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from backend.export.document import build_set
from backend.export.provenance import transcript_text_hash
from backend.export.snapshot import build_snapshot
from backend.tests.jfw4_sources import (
    req,
    req_jfw11,
    sources_full,
    sources_jfw11,
    sources_no_cover,
    sources_overlap,
    sources_partial,
    sources_unicode,
)

GOLDEN_PATH = Path(__file__).resolve().parent / "fixtures" / "jfw4" / "golden_hashes.json"
U_TEXT = "Größe 🙂 مرحبا"

CASES = {
    "vollstaendig": (
        req(formats=("json", "srt", "vtt")),
        sources_full,
    ),
    "teilweise_turngenau": (
        req(formats=("json", "srt", "vtt"), jfw2_status="partially_aligned",
            jfw3_status="partially_diarized"),
        sources_partial,
    ),
    "jfw11_dedup_namen": (
        req_jfw11(formats=("json", "srt", "vtt"), name_policy="confirmed_names"),
        sources_jfw11,
    ),
    "unicode_rtl": (
        req(formats=("json", "srt", "vtt"),
            transcript_text_hash=transcript_text_hash(U_TEXT)),
        sources_unicode,
    ),
    "ohne_cover_blockiert": (
        req(formats=("json", "srt"), jfw2_status="partially_aligned",
            jfw3_status="partially_diarized"),
        sources_no_cover,
    ),
    "overlap": (
        req(formats=("json", "srt", "vtt")),
        sources_overlap,
    ),
}


def _hashes(request, sources_factory):
    built = build_set(build_snapshot(request, sources_factory()), request)
    out = {
        role: hashlib.sha256(data).hexdigest()
        for role, data in (
            (name.rsplit(".", 1)[1], data) for name, data in built["files"].items()
        )
    }
    out["_result_hash"] = built["document"]["result_hash"]
    return out


def test_golden_bytes_match_frozen_hashes():
    golden = json.loads(GOLDEN_PATH.read_text(encoding="utf-8"))
    for name, (request, factory) in CASES.items():
        assert _hashes(request, factory) == golden[name], f"Gold-Drift: {name}"


def test_three_runs_are_byte_identical():
    for name, (request, factory) in CASES.items():
        runs = {json.dumps(_hashes(request, factory), sort_keys=True) for _ in range(3)}
        assert len(runs) == 1, f"Nicht reproduzierbar: {name}"
