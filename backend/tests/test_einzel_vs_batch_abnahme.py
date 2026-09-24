"""JFW-5 Abnahme: Einzel-vs.-Batch-Vergleich ueber den echten Vertragskern.

Spec `features/JFW-5-batch-verarbeitung.md` AC L139 (Einzel-vs.-Batch-Abnahmesatz)
und AC L142 (Evidence-Umfang: versionierte Testquellen + Einzel-vs.-Batch-
Vergleich). Evidenz: `features/evidence/JFW-5-zielsystem-qa.md`.

REAL laeuft der Vertragskern wie im Bau:
- Discovery gegen echte Quelldateien (`LocalDiscoveryFs`; vollstaendige Bytes ->
  `content_proof_v1`), Quellen-Neubindung `verify_source_binding` vor jedem Lauf
  und Commit-Bindung `verify_consumed_bytes` ueber die vollstaendig gelesenen
  Bytes.
- Snapshot/Profil/Output (`build_snapshot`, `verify_snapshot_completeness`,
  `resolve_conflicts`), Persistenz und Exactly-once-Grenzen
  (`services/batch_contract.py`) sowie die Phasen-Adapter mit fail-closed
  Revision-Binding (`default_executors`/`_phase_executor`, `REQUIRED_COMMITS`).
- Element-Pipeline (`batch.pipeline.run_item`) und der vollstaendige
  JFW-4-Exportvertrag (`export.snapshot.build_snapshot` inkl. Bindungspruefung,
  `export.document.build_set`, `export.set_writer.write_set` inkl.
  Hash-Abgleich, `services/export_contract.submit/begin/commit`).

Zielsystem-Seams (akustische Modellaueufe JFW-7/JFW-2/JFW-3 — wie im Bau hinter
injizierbaren Hooks): deterministische, inhaltsgebundene Stand-ins aus
Korpus-Markerplan (`fixtures/jfw5_abnahme/MANIFEST.json`) + echtem
`content_proof` der Quelle. Keine Sprachinhalte: der Satz besteht aus
inhaltsfreien Signalmarkern, die Ergebnis-Payloads aus inhaltsfreien
Markertokens. Die Upstream-Payload-Aufloesung fuer den Export (offene
Zielsystem-Seam laut QA-Evidenz, Punkt 4) ist ueber den transienten Carry
geschlossen.

Messfamilie (L105/L139/L141/L142): identische Ergebnisrevisionen je Element,
JFW-4-Exportbytes byteidentisch, Exactly-once (keine Doppelverarbeitung), keine
Verluste, keine Vermischung.

Ausfuehrung: backend/.venv/Scripts/python.exe -m pytest backend/tests/test_einzel_vs_batch_abnahme.py -s
"""
from __future__ import annotations

import hashlib
import json
import shutil
import sys
import time
import wave
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from backend.batch.discovery import discover
from backend.batch.identity import content_proof, new_batch_id
from backend.batch.output import resolve_conflicts
from backend.batch.pipeline import run_item as run_element_pipeline
from backend.batch.profile import profile_hash, validate_profile
from backend.batch.provenance import canonical_hash
from backend.batch.providers.local_discovery import LocalDiscoveryFs
from backend.batch.providers.phase_executors import (
    FeatureBindingConflictError,
    default_executors,
)
from backend.batch.snapshot import (
    build_snapshot,
    verify_consumed_bytes,
    verify_snapshot_completeness,
    verify_source_binding,
)
from backend.database.models import Base
from backend.export.document import build_set
from backend.export.provenance import (
    ExportRequest,
    expected_file_names,
    transcript_text_hash,
)
from backend.export.set_writer import LocalFs, write_set
from backend.export.snapshot import build_snapshot as build_export_snapshot
from backend.services import batch_contract as store, export_contract as export_store

KORPUS = Path(__file__).resolve().parent / "fixtures" / "jfw5_abnahme"
QUELLEN = KORPUS / "quellen"
MANIFEST_PATH = KORPUS / "MANIFEST.json"
FIXED_CREATED_AT = "2026-09-24T12:00:00Z"
APPLIKATIONS_EPOCHE = "jfw5-abnahme"
TOKEN = "signalmarke"


# ---------------------------------------------------------------------------
# Korpus (versionierte Testquellen) — fail-closed gegen MANIFEST.json
# ---------------------------------------------------------------------------

def lade_korpus() -> tuple[dict, str]:
    manifest = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
    material = []
    for entry in manifest["files"]:
        path = QUELLEN / entry["name"]
        assert path.exists(), f"quelle_fehlt:{entry['name']}"
        data = path.read_bytes()
        assert len(data) == entry["size_bytes"], f"groesse_drift:{entry['name']}"
        digest = hashlib.sha256(data).hexdigest()
        assert digest == entry["sha256"], f"hash_drift:{entry['name']}"
        assert content_proof(iter([data])) == f"sha256:{digest}"
        with wave.open(str(path), "rb") as handle:
            assert handle.getframerate() == entry["sample_rate_hz"]
            assert handle.getnchannels() == entry["channels"]
        material.append({"name": entry["name"], "sha256": digest,
                         "size_bytes": entry["size_bytes"]})
    canon = json.dumps(material, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    korpus_hash = "sha256:" + hashlib.sha256(canon.encode("utf-8")).hexdigest()
    assert manifest["korpus_revision"] == "jfw5-abnahme-korpus-v1"
    return manifest, korpus_hash


def wav_dauer_ms(name: str) -> int:
    with wave.open(str(QUELLEN / name), "rb") as handle:
        return handle.getnframes() * 1000 // handle.getframerate()


def marker_payload(entry: dict) -> dict:
    """Synthetische Referenz des Plans: inhaltsfreie Markertokens mit Zeiten."""
    unaligned = set(entry["unaligned_marker_indices"])
    words: list[dict] = []
    char = 0
    for marker in entry["markers"]:
        token = entry.get("token") or TOKEN
        scharf = marker["index"] not in unaligned
        words.append({
            "word_id": f"w{marker['index'] + 1}",
            "text": token,
            "char_start": char,
            "char_end": char + len(token),
            "timing_status": "aligned" if scharf else "unaligned",
            "start_ms": marker["start_ms"] if scharf else None,
            "end_ms": marker["end_ms"] if scharf else None,
            "reason_code": None if scharf else "marker_unscharf",
        })
        char += len(token) + 1
    text = " ".join(w["text"] for w in words)
    clusters = [{"cluster_id": "quelle_01", "display_label": "Quelle 1"}]
    turns = [{
        "turn_id": "t1", "cluster_id": "quelle_01",
        "start_ms": min(m["start_ms"] for m in entry["markers"]),
        "end_ms": max(m["end_ms"] for m in entry["markers"]),
        "overlap": "nicht_ueberlappend",
        "word_ids": [w["word_id"] for w in words],
    }]
    assignments = [
        {"word_id": w["word_id"], "speaker_status": "sicher",
         "cluster_id": "quelle_01", "turn_id": "t1"}
        for w in words
    ]
    return {"text": text, "words": words, "clusters": clusters,
            "turns": turns, "word_assignments": assignments}


def jfw2_status_fuer(entry: dict) -> str:
    return "partially_aligned" if entry["unaligned_marker_indices"] else "aligned"


# ---------------------------------------------------------------------------
# Eingefrorenes Abnahmeprofil (identisch in beiden Laufarten)
# ---------------------------------------------------------------------------

def abnahme_profil(zielwurzel: str) -> dict:
    profil = {
        "language_setting": "auto",
        "stt_model": "turbo",
        "model_revision": "a" * 40,
        "decode_profile": "jfw7-transcribe-greedy-v1",
        "alignment_enabled": True,
        "alignment_profile": "precise_words_v1",
        "diarization_enabled": True,
        "speaker_mode": "auto",
        "speaker_count": None,
        "speaker_min": None,
        "speaker_max": None,
        "diarization_profile": "meeting_speakers_v1",
        "export_enabled": True,
        "export_formats": ["json", "srt", "vtt"],
        "export_profile": "lesbare_untertitel_v1",
        "name_policy": "neutral",
        "minutes_enabled": False,
        "minutes_profile": "jfw13_minutes_v1",
        "partial_failure_policy": "mit_belegten_daten_fortfahren",
        "fail_fast": False,
        "output_policy": {
            "target_root": zielwurzel,
            "structure": "relative_source",
            "conflict_rule": "blockieren",
        },
    }
    assert validate_profile(profil) == []
    return profil


# ---------------------------------------------------------------------------
# Seams (Zielsystem) + realer JFW-4-Export ueber den gebundenen Vertrag
# ---------------------------------------------------------------------------

def export_request(item: dict, profil: dict, commits: dict, carry: dict) -> ExportRequest:
    """Gebundene Exportanfrage — Muster `_run_export`/`routes/export.py`."""
    upstream = commits["transcribe"]
    return ExportRequest(
        job_id=item["item_id"],
        audio_asset_id=item["source"]["path"],
        audio_hash=str(item["source"]["content_proof"]).removeprefix("sha256:"),
        audio_duration_ms=int(carry["audio_duration_ms"]),
        timebase="audio_ms_v1",
        transcript_run_id=upstream["run_id"],
        transcript_revision_id=upstream.get("revision_id") or upstream["result_hash"],
        transcript_revision_hash=upstream["result_hash"],
        transcript_text_hash=upstream["result_hash"],
        jfw2_result_hash=commits["align"]["result_hash"],
        jfw2_status=carry["jfw2_status"],
        jfw3_result_hash=commits["diarize"]["result_hash"],
        jfw3_status=carry["jfw3_status"],
        formats=tuple(profil.get("export_formats") or ("json",)),
        export_profile=profil.get("export_profile") or "lesbare_untertitel_v1",
        name_policy=profil.get("name_policy") or "neutral",
        target_dir=item.get("output", {}).get("target_path"),
    )


def seam_hooks(session, entry: dict, zaehler: dict) -> dict:
    """Injizierbare Feature-Hooks: akustische Seams + realer JFW-4-Vertrag."""
    plan = marker_payload(entry)
    teilweise = bool(entry["unaligned_marker_indices"])

    def zaehle(phase: str, item_id: str) -> None:
        zaehler[(phase, item_id)] = zaehler.get((phase, item_id), 0) + 1

    def transcribe(ctx):
        item = ctx["item"]
        zaehle("transcribe", item["item_id"])
        proof = item["source"]["content_proof"]
        run_id = "jfw7-abnahme-" + canonical_hash(
            {"content_proof": proof, "decode": ctx["profile"]["decode_profile"]}
        )[:12]
        return {
            "commit_ref": {"feature": "jfw7", "run_id": run_id,
                           "result_hash": transcript_text_hash(plan["text"])},
            "status": "succeeded",
            "warnings": [],
            "carry": {"transcript_text": plan["text"],
                      "audio_duration_ms": wav_dauer_ms(entry["name"]),
                      "jfw2_words": plan["words"]},
        }

    def align(ctx):
        item = ctx["item"]
        zaehle("align", item["item_id"])
        upstream = ctx["commits"]["transcribe"]
        result_hash = canonical_hash({
            "feature": "jfw2", "content_proof": item["source"]["content_proof"],
            "transcript": upstream["result_hash"], "words": plan["words"]})
        return {
            "commit_ref": {"feature": "jfw2",
                           "identity_hash": canonical_hash({
                               "job": item["item_id"],
                               "transcript": upstream["result_hash"]}),
                           "result_hash": result_hash},
            "status": "partial" if teilweise else "succeeded",
            "warnings": ["teilweise_ausgerichtet"] if teilweise else [],
            "carry": {"jfw2_status": jfw2_status_fuer(entry)},
        }

    def diarize(ctx):
        item = ctx["item"]
        zaehle("diarize", item["item_id"])
        upstream = ctx["commits"]["transcribe"]
        result_hash = canonical_hash({
            "feature": "jfw3", "content_proof": item["source"]["content_proof"],
            "transcript": upstream["result_hash"],
            "clusters": plan["clusters"], "turns": plan["turns"],
            "word_assignments": plan["word_assignments"]})
        return {
            "commit_ref": {"feature": "jfw3",
                           "identity_hash": canonical_hash({
                               "job": item["item_id"],
                               "transcript": upstream["result_hash"]}),
                           "result_hash": result_hash},
            "status": "succeeded",
            "warnings": [],
            "carry": {"jfw3_status": "diarized"},
        }

    def export(ctx):
        item, profil, commits = ctx["item"], ctx["profile"], ctx["commits"]
        zaehle("export", item["item_id"])
        carry = dict(ctx["carry"])
        carry.setdefault("jfw2_status", jfw2_status_fuer(entry))
        carry.setdefault("jfw3_status", "diarized")
        request = export_request(item, profil, commits, carry)
        sources = {
            "text": carry["transcript_text"],
            "jfw2": {"result_hash": commits["align"]["result_hash"],
                     "status": carry["jfw2_status"], "words": plan["words"]},
            "jfw3": {"result_hash": commits["diarize"]["result_hash"],
                     "status": carry["jfw3_status"],
                     "clusters": plan["clusters"], "turns": plan["turns"],
                     "word_assignments": plan["word_assignments"]},
            "segments": [],
            "jfw11": None,
        }
        snap = build_export_snapshot(request, sources)
        if snap.binding_status != "ok":
            raise FeatureBindingConflictError(str(snap.binding_status))
        names = expected_file_names(request)
        submitted = export_store.submit_export(session, request, names, snap.readiness)
        if submitted["outcome"] == "conflict":
            raise FeatureBindingConflictError("export_conflict")
        attempt = export_store.begin_export(session, submitted["export_key"],
                                            app_epoch=APPLIKATIONS_EPOCHE)
        if attempt is None:
            raise FeatureBindingConflictError("export_nicht_startbereit")
        built = build_set(snap, request)
        expected_hashes = {
            name: hashlib.sha256(data).hexdigest()
            for name, data in built["files"].items()
        }
        write_set(LocalFs(), request.target_dir, built["files"], expected_hashes,
                  attempt_id=attempt, replace=False)
        outcome = export_store.commit_set(
            session, submitted["export_key"],
            manifest=built["document"]["presentation"],
            result_hash=built["document"]["result_hash"],
        )
        if outcome != "committed":
            raise FeatureBindingConflictError(str(outcome))
        return {
            "commit_ref": {"feature": "jfw4", "export_key": submitted["export_key"],
                           "result_hash": built["document"]["result_hash"]},
            "status": "succeeded",
            "warnings": [],
        }

    def minutes(ctx):
        raise AssertionError("minutes nicht im Abnahmeprofil")

    return {"transcribe": transcribe, "align": align, "diarize": diarize,
            "export": export, "minutes": minutes}


# ---------------------------------------------------------------------------
# Laufarten ueber den echten Vertragskern (Ablauf wie `routes/batch.py`)
# ---------------------------------------------------------------------------

def neue_session():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False},
                           poolclass=StaticPool)
    Base.metadata.create_all(engine)
    factory = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    return factory(), engine


def bestaetige(session, disc: dict, profil: dict) -> tuple[str, dict]:
    snap = build_snapshot(
        batch_id=new_batch_id(), discovery=disc, profile=profil,
        revision_no=1, parent_snapshot_hash=None, created_at=FIXED_CREATED_AT,
    )
    assert verify_snapshot_completeness(snap) == []
    resolved = resolve_conflicts(snap["items"], dict(profil["output_policy"]),
                                 existing_targets=())
    assert resolved["ok"], resolved["reason_code"]
    for item, assignment in zip(snap["items"], resolved["assignments"], strict=True):
        item["output"] = dict(assignment["output"])
    out = store.submit_batch(session, snap, app_epoch=APPLIKATIONS_EPOCHE)
    assert out["outcome"] == "created"
    assert store.start_batch(session, out["identity_hash"]) == "running"
    return out["identity_hash"], snap


def verarbeite(session, identity_hash: str, item: dict, profil: dict, hooks: dict) -> dict:
    source = dict(item["source"])
    binding = verify_source_binding({"source": source}, LocalDiscoveryFs())
    assert binding["ok"], binding
    consumed = content_proof(LocalDiscoveryFs().read_chunks(source["path"]))
    assert verify_consumed_bytes({"source": source}, consumed)["ok"]
    begun = store.begin_item_attempt(session, identity_hash, item["item_id"],
                                     app_epoch=APPLIKATIONS_EPOCHE)
    assert begun["outcome"] == "started"
    attempt_id = begun["attempt_id"]
    execs = default_executors(session=session, hooks=hooks)

    def guarded(phase, ctx):
        outcome = execs[phase](phase, ctx)
        if getattr(outcome, "commit_ref", None):
            store.record_phase_commit(session, attempt_id, phase, outcome.commit_ref)
        return outcome

    result = run_element_pipeline(
        item={"item_id": item["item_id"], "source": source,
              "relative_path": item["relative_path"],
              "output": dict(item["output"])},
        profile=dict(profil),
        executors={phase: guarded for phase in execs},
    )
    assert store.finalize_item_attempt(
        session, attempt_id, result["end_state"], reason_code=None,
        phase_states={p: info["status"] for p, info in result["phases"].items()},
    ) == "finalized"
    return result


def lese_exportdateien(item: dict) -> dict:
    ziel = LocalFs()
    verzeichnis = item["output"]["target_path"]
    dateien = {}
    for name in sorted(ziel.listdir(verzeichnis)):
        assert not name.startswith(".jfw4-"), f"temp_rest:{name}"
        dateien[name] = ziel.read_bytes(f"{verzeichnis}/{name}")
    return dateien


def exactly_once_proben(session, entry: dict, identity_hash: str, item: dict,
                        profil: dict, commits: dict) -> None:
    """Keine Doppelverarbeitung: Batch-Store und Export-Store sind einmalig."""
    nochmal = store.begin_item_attempt(session, identity_hash, item["item_id"],
                                       app_epoch=APPLIKATIONS_EPOCHE)
    assert nochmal["outcome"] == "not_startable"
    detail = store.get_batch(session, identity_hash)
    versuche = [a for a in detail["attempts"] if a["item_id"] == item["item_id"]]
    assert len(versuche) == 1
    request = export_request(
        item, profil, commits,
        {"audio_duration_ms": wav_dauer_ms(entry["name"]),
         "jfw2_status": jfw2_status_fuer(entry), "jfw3_status": "diarized"},
    )
    erneut = export_store.submit_export(session, request, expected_file_names(request),
                                        {"state": "ready"})
    assert erneut["outcome"] == "existing"
    assert export_store.begin_export(session, erneut["export_key"],
                                     app_epoch=APPLIKATIONS_EPOCHE) is None


def lauf(manifest: dict, profil: dict, zaehler: dict, modus: str) -> dict:
    session, engine = neue_session()
    try:
        ergebnisse = {}
        plaene = {e["name"]: e for e in manifest["files"]}
        if modus == "einzel":
            auswahlen = [[{"kind": "file", "path": str(QUELLEN / e["name"])}]
                         for e in manifest["files"]]
        else:
            auswahlen = [[{"kind": "folder", "path": str(QUELLEN)}]]
        for auswahl in auswahlen:
            disc = discover(auswahl, LocalDiscoveryFs())
            identity_hash, snap = bestaetige(session, disc, profil)
            for item in snap["items"]:
                name = Path(item["source"]["path"].replace("\\", "/")).name
                entry = plaene[name]
                result = verarbeite(session, identity_hash, item, profil,
                                    seam_hooks(session, entry, zaehler))
                exactly_once_proben(session, entry, identity_hash, item, profil,
                                    result["commits"])
                ergebnisse[item["item_id"]] = {
                    "name": name,
                    "end_state": result["end_state"],
                    "phases": {p: info["status"] for p, info in result["phases"].items()},
                    "commits": result["commits"],
                    "content_proof": item["source"]["content_proof"],
                    "dateien": lese_exportdateien(item),
                }
            if modus == "batch":
                detail = store.get_batch(session, identity_hash)
                for eintrag in detail["attempts"]:
                    assert eintrag["status"] in ("succeeded", "succeeded_with_warnings")
                abschluss = store.finalize_batch(session, identity_hash)
                assert abschluss in ("completed", "completed_with_issues")
        return ergebnisse
    finally:
        session.close()
        engine.dispose()


# ---------------------------------------------------------------------------
# Abnahme-Tests
# ---------------------------------------------------------------------------

def test_korpus_manifest_bindet_alle_quellen_fail_closed():
    manifest, korpus_hash = lade_korpus()
    assert len(manifest["files"]) == 8
    assert all((QUELLEN / e["name"]).exists() for e in manifest["files"])
    assert korpus_hash.startswith("sha256:")
    assert sum(e["size_bytes"] for e in manifest["files"]) > 0


def test_jfw4_bindungspruefung_ist_fail_closed():
    """Sichtbare Pruefung der JFW-2/JFW-3-/JFW-4-Bindungen: Abweichung sperrt."""
    manifest, _ = lade_korpus()
    entry = manifest["files"][0]
    plan = marker_payload(entry)
    profil = abnahme_profil("C:/ziel")
    item = {"item_id": "jfw5-item-0000000000000001",
            "source": {"path": "C:/data/q.wav", "content_proof": "sha256:" + "0" * 64},
            "output": {"target_path": "C:/ziel/q"}}
    commits = {"transcribe": {"feature": "jfw7", "run_id": "run-1",
                              "result_hash": transcript_text_hash(plan["text"])},
               "align": {"feature": "jfw2", "result_hash": "1" * 40},
               "diarize": {"feature": "jfw3", "result_hash": "2" * 40}}
    carry = {"transcript_text": plan["text"], "audio_duration_ms": 1000,
             "jfw2_status": "aligned", "jfw3_status": "diarized"}
    request = export_request(item, profil, commits, carry)
    scharf = build_export_snapshot(request, {
        "text": plan["text"],
        "jfw2": {"result_hash": "1" * 40, "status": "aligned", "words": plan["words"]},
        "jfw3": {"result_hash": "2" * 40, "status": "diarized",
                 "clusters": plan["clusters"], "turns": plan["turns"],
                 "word_assignments": plan["word_assignments"]},
        "segments": [], "jfw11": None,
    })
    assert scharf.binding_status == "ok"
    falsch = build_export_snapshot(request, {
        "text": plan["text"],
        "jfw2": {"result_hash": "FALSCH", "status": "aligned", "words": plan["words"]},
        "jfw3": {"result_hash": "2" * 40, "status": "diarized",
                 "clusters": plan["clusters"], "turns": plan["turns"],
                 "word_assignments": plan["word_assignments"]},
        "segments": [], "jfw11": None,
    })
    assert falsch.binding_status == "binding_conflict"
    assert falsch.readiness["state"] == "blocked"


def test_einzel_vs_batch_liefern_identische_ergebnisse(tmp_path):
    manifest, korpus_hash = lade_korpus()
    ziel = tmp_path / "ziel"
    ziel.mkdir()
    profil = abnahme_profil(str(ziel))
    p_hash = profile_hash(profil)

    zaehler_einzel: dict = {}
    start = time.perf_counter()
    einzel = lauf(manifest, profil, zaehler_einzel, "einzel")
    dauer_einzel = time.perf_counter() - start

    # Identisches Profil, identischer Zielstamm, leerer Zielstand fuer Lauf 2.
    shutil.rmtree(ziel)
    ziel.mkdir()

    zaehler_batch: dict = {}
    start = time.perf_counter()
    batch = lauf(manifest, profil, zaehler_batch, "batch")
    dauer_batch = time.perf_counter() - start

    # 1) Keine Verluste: beide Laufarten verarbeiten exakt den Satz.
    assert len(einzel) == len(batch) == len(manifest["files"])
    assert set(einzel) == set(batch)

    zeilen = []
    byteidentisch_gesamt = 0
    for item_id in sorted(einzel, key=lambda i: einzel[i]["name"]):
        e, b = einzel[item_id], batch[item_id]
        # 2) Identische Ergebnisrevisionen je Element und Phase.
        assert e["commits"] == b["commits"], item_id
        assert e["end_state"] == b["end_state"], item_id
        assert e["phases"] == b["phases"], item_id
        # 3) JFW-4-Exportbytes byteidentisch (Vertrag: unabhaengig vom Dateiziel).
        assert set(e["dateien"]) == set(b["dateien"]), item_id
        for name, daten in e["dateien"].items():
            assert daten == b["dateien"][name], f"{item_id}:{name}"
            byteidentisch_gesamt += 1
        # 4) Keine Vermischung: Dokument bindet die eigene Quelle.
        for daten in e["dateien"].values():
            if not daten.lstrip().startswith(b"{"):
                continue
            doc = json.loads(daten.decode("utf-8"))
            assert doc["job"]["job_id"] == item_id
            assert doc["job"]["audio_hash"] == e["content_proof"].removeprefix("sha256:")
        zeilen.append((e["name"], item_id, e, b))

    # 5) Exactly-once je Phase und Element (keine Doppelverarbeitung).
    assert set(zaehler_einzel) == set(zaehler_batch)
    for schluessel, anzahl in zaehler_einzel.items():
        assert anzahl == 1, schluessel
        assert zaehler_batch[schluessel] == 1, schluessel

    # 6) Endzuustaende erwartbar und ueber beide Laufarten gleich.
    zustaende: dict = {}
    for _, _, e, _ in zeilen:
        zustaende[e["end_state"]] = zustaende.get(e["end_state"], 0) + 1
    assert zustaende == {"succeeded": 7, "succeeded_with_warnings": 1}

    # Bericht fuer die Evidenz (features/evidence/JFW-5-zielsystem-qa.md).
    print("\n== JFW-5 Einzel-vs.-Batch-Vergleich (Abnahmesatz) ==")
    print(f"Korpus-Revision: {manifest['korpus_revision']} · Korpus-Hash: {korpus_hash}")
    print(f"Profil-Hash: {p_hash} · Quellen: {len(zeilen)}")
    print("| Element (Datei) | item_id | transcribe | align | diarize | export | "
          "JFW-4 byteidentisch | Endzustand |")
    print("|---|---|---|---|---|---|---|---|")
    for name, item_id, e, b in zeilen:
        k = e["commits"]
        ident = "ja" if e["dateien"] == b["dateien"] else "NEIN"
        print(f"| {name} | {item_id} | {k['transcribe']['result_hash'][:12]} | "
              f"{k['align']['result_hash'][:12]} | {k['diarize']['result_hash'][:12]} | "
              f"{k['export']['result_hash'][:12]} | {ident} | "
              f"{e['end_state']} / {b['end_state']} |")
    for _, _, e, _ in zeilen:
        for dateiname, daten in sorted(e["dateien"].items()):
            print(f"|   └ {dateiname} | sha256:{hashlib.sha256(daten).hexdigest()} | "
                  f"{len(daten)} Bytes |")
    print(f"Byteidentisch verglichene Exportdateien: {byteidentisch_gesamt} Paare "
          f"(Einzel-Bytes == Batch-Bytes je Datei; {len(zeilen)} Elemente x 3 Formate)")
    print(f"Laufzeit Einzel: {dauer_einzel:.3f} s · Batch: {dauer_batch:.3f} s")
