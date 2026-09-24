"""JFW-5-Abnahme-Korpus: deterministische, inhaltsfreie Testquellen (WAV/PCM16).

Erzeugt den gefrorenen Testsatz ``jfw5-abnahme-korpus-v1`` fuer den
Einzel-vs.-Batch-Vergleich (Spec `features/JFW-5-batch-verarbeitung.md`,
AC L139/L142). Regeln:

* ECHT VERARBEITBARE Quellen: gueltige RIFF/WAVE-PCM-Dateien im Format, das die
  Batch-Pipeline real als Quelle akzeptiert (``SUPPORTED_MEDIA_EXTENSIONS`` der
  Discovery). Keine Text-Platzhalter, keine erfundenen Formate.
* INHALTSFREI/SYNTHETISCH: digitale Rechteck-Marker auf digitaler Stille —
  keine Sprache, keine Personen, keine echten Aufnahmen (Abgrenzung wie im
  JFW-11-Capture-Spike: „generierte Marker.Toene, keine Gespraeche").
* DETERMINISTISCH: ganzzahlige Wellenform (Rechteck, keine Float-Trigonometrie),
  feste Kopfbytes des ``wave``-Moduls; `--verify` reproduziert die Bytes
  bitidentisch und prueft Groesse/SHA-256 gegen ``MANIFEST.json``.
* VERSIONIERT: ``quellen/*.wav`` + ``MANIFEST.json`` (Korpus-Revision,
  Markerplan, SHA-256 je Datei) liegen im Repo; der Markerplan ist die
  synthetische Referenz fuer die Seams des Vergleichslaufs.

Aufruf (aus diesem Verzeichnis, beliebiger Python-3-Interpreter, nur stdlib):
    python generate_korpus.py            # erzeugen + Manifest schreiben
    python generate_korpus.py --verify   # bitidentisch verifizieren (Exit != 0 bei Drift)
"""
from __future__ import annotations

import argparse
import hashlib
import io
import json
import sys
import wave
from pathlib import Path

ROOT = Path(__file__).resolve().parent
QUELLEN = ROOT / "quellen"
MANIFEST_PATH = ROOT / "MANIFEST.json"

TOKEN = "signalmarke"
KORPUS_ID = "jfw5-abnahme-korpus"
KORPUS_REVISION = "jfw5-abnahme-korpus-v1"
CONTENT_PROOF = "content_proof_v1"
CREATED_AT = "2026-09-24"
AMPLITUDE = 9000
SAMPLE_WIDTH_BYTES = 2

#: (name, sample_rate_hz, channels, duration_ms, marker(start_ms, end_ms), unaligned)
SPECS = [
    {
        "name": "q01_8k_mono_0k6_m2.wav",
        "sample_rate_hz": 8000,
        "channels": 1,
        "duration_ms": 600,
        "markers": [(100, 240), (350, 490)],
        "unaligned_marker_indices": [],
        "beschreibung": "Zwei Signalmarker, 8 kHz mono",
    },
    {
        "name": "q02_16k_mono_1k0_m3.wav",
        "sample_rate_hz": 16000,
        "channels": 1,
        "duration_ms": 1000,
        "markers": [(150, 280), (450, 580), (750, 880)],
        "unaligned_marker_indices": [],
        "beschreibung": "Drei Signalmarker, 16 kHz mono",
    },
    {
        "name": "q03_44k_stereo_0k8_m2.wav",
        "sample_rate_hz": 44100,
        "channels": 2,
        "duration_ms": 800,
        "markers": [(120, 260), (500, 640)],
        "unaligned_marker_indices": [],
        "beschreibung": "Zwei Signalmarker, 44,1 kHz stereo (anderer Container)",
    },
    {
        "name": "q04_8k_mono_1k2_m4.wav",
        "sample_rate_hz": 8000,
        "channels": 1,
        "duration_ms": 1200,
        "markers": [(100, 220), (350, 470), (600, 720), (850, 970)],
        "unaligned_marker_indices": [],
        "beschreibung": "Vier Signalmarker, laengste Quelle des Satzes",
    },
    {
        "name": "q05_16k_mono_0k5_m2.wav",
        "sample_rate_hz": 16000,
        "channels": 1,
        "duration_ms": 500,
        "markers": [(80, 200), (300, 420)],
        "unaligned_marker_indices": [],
        "beschreibung": "Zwei Signalmarker, kuerzeste Quelle des Satzes",
    },
    {
        "name": "q06_8k_mono_0k9_m3_partial.wav",
        "sample_rate_hz": 8000,
        "channels": 1,
        "duration_ms": 900,
        "markers": [(100, 240), (400, 540), (700, 840)],
        "unaligned_marker_indices": [1],
        "beschreibung": "Drei Marker, Marker 2 im Plan als unscharf (unaligned) "
                        "deklariert -> Teilqualitaet in beiden Laufarten",
    },
    {
        "name": "q07_44k_mono_0k7_m2.wav",
        "sample_rate_hz": 44100,
        "channels": 1,
        "duration_ms": 700,
        "markers": [(150, 300), (450, 600)],
        "unaligned_marker_indices": [],
        "beschreibung": "Zwei Signalmarker, 44,1 kHz mono",
    },
    {
        "name": "q08_16k_mono_1k1_m3.wav",
        "sample_rate_hz": 16000,
        "channels": 1,
        "duration_ms": 1100,
        "markers": [(120, 260), (450, 590), (800, 940)],
        "unaligned_marker_indices": [],
        "beschreibung": "Drei Signalmarker, 16 kHz mono",
    },
]


def _half_period(sample_rate_hz: int) -> int:
    return max(1, sample_rate_hz // 1000)


def build_wav(spec: dict) -> bytes:
    """Erzeugt die Quelldatei bitidentisch reproduzierbar (nur Ganzzahlen)."""
    rate = int(spec["sample_rate_hz"])
    channels = int(spec["channels"])
    total = rate * int(spec["duration_ms"]) // 1000
    half = _half_period(rate)
    marker_frames = [
        (int(start) * rate // 1000, int(end) * rate // 1000)
        for start, end in spec["markers"]
    ]
    samples = []
    for i in range(total):
        value = 0
        for start, end in marker_frames:
            if start <= i < end:
                phase = (i - start) // half
                value = AMPLITUDE if phase % 2 == 0 else -AMPLITUDE
                break
        samples.extend([value] * channels)
    payload = b"".join(int(v).to_bytes(SAMPLE_WIDTH_BYTES, "little", signed=True)
                       for v in samples)
    buf = io.BytesIO()
    with wave.open(buf, "wb") as handle:
        handle.setnchannels(channels)
        handle.setsampwidth(SAMPLE_WIDTH_BYTES)
        handle.setframerate(rate)
        handle.writeframes(payload)
    return buf.getvalue()


def manifest_entry(spec: dict, data: bytes) -> dict:
    return {
        "name": spec["name"],
        "size_bytes": len(data),
        "sha256": hashlib.sha256(data).hexdigest(),
        "sample_rate_hz": int(spec["sample_rate_hz"]),
        "channels": int(spec["channels"]),
        "sample_width_bytes": SAMPLE_WIDTH_BYTES,
        "duration_ms": int(spec["duration_ms"]),
        "token": TOKEN,
        "markers": [
            {"index": index, "start_ms": int(start), "end_ms": int(end)}
            for index, (start, end) in enumerate(spec["markers"])
        ],
        "unaligned_marker_indices": list(spec["unaligned_marker_indices"]),
        "beschreibung": spec["beschreibung"],
    }


def build_manifest() -> dict:
    entries = [manifest_entry(spec, build_wav(spec)) for spec in SPECS]
    return {
        "korpus_id": KORPUS_ID,
        "korpus_revision": KORPUS_REVISION,
        "content_proof_contract": CONTENT_PROOF,
        "created_at": CREATED_AT,
        "generator": "generate_korpus.py (ganzzahlige Rechteck-Marker, deterministisch)",
        "quellformat": "RIFF/WAVE PCM16 (unterstuetzt laut backend/batch/discovery.py)",
        "datenschutz": (
            "synthetisch und inhaltsfrei: digitale Signalmarker auf digitaler "
            "Stille, keine Sprache, keine echten Aufnahmen, kein Personenbezug"
        ),
        "verwendung": (
            "gefrorener Abnahmesatz fuer den Einzel-vs.-Batch-Vergleich "
            "(backend/tests/test_einzel_vs_batch_abnahme.py); der Markerplan ist "
            "die synthetische Referenz der akustischen Zielsystem-Seams"
        ),
        "files": entries,
    }


def korpus_hash(manifest: dict) -> str:
    material = [
        {"name": e["name"], "sha256": e["sha256"], "size_bytes": e["size_bytes"]}
        for e in manifest["files"]
    ]
    canon = json.dumps(material, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return "sha256:" + hashlib.sha256(canon.encode("utf-8")).hexdigest()


def write() -> int:
    manifest = build_manifest()
    QUELLEN.mkdir(parents=True, exist_ok=True)
    for spec in SPECS:
        (QUELLEN / spec["name"]).write_bytes(build_wav(spec))
    MANIFEST_PATH.write_text(
        json.dumps(manifest, indent=2, ensure_ascii=False, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    print(f"Korpus {KORPUS_REVISION}: {len(SPECS)} Quellen geschrieben")
    print(f"Korpus-Hash: {korpus_hash(manifest)}")
    return 0


def verify() -> int:
    if not MANIFEST_PATH.exists():
        print("FEHLER: MANIFEST.json fehlt", file=sys.stderr)
        return 1
    on_disk = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
    fresh = build_manifest()
    errors = []
    if on_disk.get("korpus_revision") != KORPUS_REVISION:
        errors.append("korpus_revision_veraendert")
    if on_disk != fresh:
        errors.append("manifest_abweichend")
    for spec in SPECS:
        path = QUELLEN / spec["name"]
        if not path.exists():
            errors.append(f"quelle_fehlt:{spec['name']}")
            continue
        data = path.read_bytes()
        expected = build_wav(spec)
        if data != expected:
            errors.append(f"quelle_nicht_bitidentisch:{spec['name']}")
        entry = manifest_entry(spec, data)
        if entry["sha256"] != hashlib.sha256(data).hexdigest():
            errors.append(f"hash_drift:{spec['name']}")
    if errors:
        for err in errors:
            print(f"FEHLER: {err}", file=sys.stderr)
        return 1
    print(f"OK: {KORPUS_REVISION} bitidentisch reproduziert")
    print(f"Korpus-Hash: {korpus_hash(fresh)}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--verify", action="store_true",
                        help="nur verifizieren, nichts schreiben")
    args = parser.parse_args()
    return verify() if args.verify else write()


if __name__ == "__main__":
    sys.exit(main())
